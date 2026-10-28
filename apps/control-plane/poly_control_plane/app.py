from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, SQLModel, create_engine, delete, select

from poly_agent import Supervisor
from poly_contracts import (
    AreaWriteRequest,
    DataType,
    DeployPlan,
    LinkOptions,
    Protocol,
    ServerConfig,
    ServerCreate,
    ServerOptions,
    ServerUpdate,
    SimMode,
    SimSpec,
    SiteAccessProbe,
    SiteCreate,
    SiteProbe,
    SiteProbeRequest,
    SiteUpdate,
    TagDef,
    WordOrder,
    WriteRequest,
)
from poly_core import can_bind, validate_tags

from . import secret as secret_store
from .db import AuditRow, ServerRow, SiteRow, TagRow
from .deploy import DeployError, RemoteSupervisor, decide_carrier
from .link import LinkSupervisor, LinkTarget
from .link_clients import (
    LinkConnectionError,
    LinkDeviceError,
    LinkError,
    build_link_client,
)
from .migrate import ensure_columns
from .ssh import (
    DEFAULT_DEPLOY_DIR,
    SshError,
    SshRunner,
    SshSessionPool,
    SshTarget,
    probe_local,
    run_probe,
)
from .system import DIST, ROOT, data_dir, system_info

DB_PATH = data_dir() / "app.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

# One SSH connection per site, reused across polls (see ssh.SshSessionPool).
SSH_POOL = SshSessionPool()


def get_session() -> Any:
    with Session(engine) as session:
        yield session



# ------------------------------------------------------------ runtime routing
def _probe_of(row: SiteRow) -> SiteProbe | None:
    """The last probe of a site, or None when it has never been probed."""
    if not row.last_probe:
        return None
    try:
        return SiteProbe(**row.last_probe)
    except Exception:
        return None


def _site_deploy(row: SiteRow) -> dict[str, Any]:
    """How instances on this site will be run - reported, never chosen."""
    if row.is_local:
        return DeployPlan(
            carrier="native",
            label="本机进程",
            reason="这个 site 没有填 host，实例直接在这台机器上起进程。",
            action="起进程",
            python=sys.executable,
            ready=True,
        ).model_dump()
    return decide_carrier(_probe_of(row)).model_dump()


def _remote_supervisor(app: FastAPI, site: SiteRow) -> RemoteSupervisor:
    """The (cached) supervisor for a remote site.

    One per site, because one SSH connection per site is the whole point of
    the pool. The password is decrypted here and never leaves the process.
    """
    cache: dict[str, RemoteSupervisor] = app.state.remote_supervisors
    existing = cache.get(site.id)
    if existing is not None:
        return existing
    # A site that is down must not cost a fresh 10 s handshake per instance on
    # every poll; remember the failure briefly and answer from memory.
    failed = app.state.remote_errors.get(site.id)
    if failed and time.time() < failed[0]:
        raise SshError(failed[1])
    password = secret_store.decrypt_secret(data_dir(), site.ssh_secret)
    target = SshTarget(
        host=site.host, port=site.ssh_port, user=site.ssh_user, password=password
    )

    def factory() -> SshRunner:
        return SSH_POOL.acquire(site.id, target)

    try:
        factory()  # prove the machine answers before we hand anything back
    except SshError as exc:
        app.state.remote_errors[site.id] = (time.time() + 30, str(exc))
        raise
    supervisor = RemoteSupervisor(factory, site.deploy_dir or DEFAULT_DEPLOY_DIR)
    cache[site.id] = supervisor
    return supervisor


def _runtime(app: FastAPI, session: Session, site_id: str) -> tuple[Any, SiteRow]:
    """(supervisor, site) - the local one, or the SSH one for that machine."""
    site = _site(session, site_id)
    if site.is_local:
        return app.state.supervisor, site
    return _remote_supervisor(app, site), site


def _link_tags(session: Session, row: ServerRow) -> list[TagDef]:
    """The address table a link reads on the far side.

    Modbus links have none (their data area is addressed directly); S7 links
    define which DB/M/I/Q addresses the page should read and write.
    """
    if row.protocol == Protocol.MODBUS_TCP.value:
        return []
    return [_tag_def(tag) for tag in _tag_rows(session, row.id)]


def _link_target(row: ServerRow, tags: list[TagDef] | None = None) -> LinkTarget:
    options = LinkOptions(**row.target_options) if row.target_options else LinkOptions()
    return LinkTarget(
        host=row.target_host.strip(),
        port=row.target_port or row.port,
        protocol=Protocol(row.protocol),
        unit_id=options.unit_id,
        rack=options.rack,
        slot=options.slot,
        timeout=options.timeout_ms / 1000.0,
        tags=list(tags or []),
        namespace_index=options.namespace_index,
    )


def _link_target_from_body(body: ServerCreate) -> LinkTarget:
    """The same shape, straight from a create request (no row, no tags yet)."""
    options = body.target_options
    return LinkTarget(
        host=body.target_host.strip(),
        port=body.target_port,
        protocol=body.protocol,
        unit_id=options.unit_id,
        rack=options.rack,
        slot=options.slot,
        timeout=options.timeout_ms / 1000.0,
        namespace_index=options.namespace_index,
    )


def _link_supervisor(app: FastAPI, session: Session, row: ServerRow) -> LinkSupervisor:
    """The (cached) client connection behind a link instance.

    Cached by instance id because the socket is the expensive part: a new
    supervisor per request would reconnect on every poll. The tag table is
    refreshed in place, so editing a tag does not drop a healthy connection.
    """
    cache: dict[str, LinkSupervisor] = app.state.link_supervisors
    tags = _link_tags(session, row)
    existing = cache.get(row.id)
    if existing is not None:
        existing.set_tags(tags)
        return existing
    supervisor = LinkSupervisor(
        row.id,
        build_link_client(Protocol(row.protocol)),
        tags=tags,
    )
    cache[row.id] = supervisor
    return supervisor


def _launch(
    app: FastAPI, session: Session, row: ServerRow, restart: bool = False
) -> dict[str, Any]:
    """Start (or restart) one instance wherever it lives."""
    try:
        if row.is_link:
            if not row.target_host.strip():
                raise ValueError("还没有填目标地址")
            link = _link_supervisor(app, session, row)
            if restart:
                link.stop()
            state = link.start(_link_target(row, _link_tags(session, row)))
        else:
            supervisor, site = _runtime(app, session, row.site_id)
            config = _to_config(session, row)
            if site.is_local:
                state = supervisor.restart(config) if restart else supervisor.start(config)
            else:
                probe = _probe_of(site)
                state = (
                    supervisor.restart(config, probe)
                    if restart
                    else supervisor.start(config, probe)
                )
        return state.model_dump(mode="json")
    except Exception as exc:
        # A deployment failure (no python, port taken on the target, ...) has to
        # reach the UI as the instance's error, not as a 500.
        return {"status": "error", "error": str(exc)}


def _state_of(app: FastAPI, session: Session, row: ServerRow) -> dict[str, Any]:
    # A disabled link must not even be *asked* for state: LinkSupervisor.state()
    # can probe, and a probe after stop() would silently reconnect the client.
    if not row.enabled:
        return {"status": "stopped"}
    if row.is_link:
        return _link_supervisor(app, session, row).state().model_dump(mode="json")
    supervisor, _ = _runtime(app, session, row.site_id)
    return supervisor.state(row.id).model_dump(mode="json")


def _safe_state(app: FastAPI, session: Session, row: ServerRow) -> dict[str, Any]:
    try:
        return _state_of(app, session, row)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _stop(app: FastAPI, session: Session, row: ServerRow) -> None:
    if row.is_link:
        try:
            _link_supervisor(app, session, row).stop()
        except Exception:
            pass
        return
    supervisor, _ = _runtime(app, session, row.site_id)
    try:
        supervisor.stop(row.id)
    except Exception:
        pass


def _forget(app: FastAPI, session: Session, row: ServerRow) -> None:
    """Stop an instance and drop everything the machine kept for it."""
    if row.is_link:
        try:
            _link_supervisor(app, session, row).forget()
        except Exception:
            pass
        # The supervisor holds a client plus an event deque; dropping the cache
        # entry is what stops a deleted instance from being remembered. Without
        # it the map grows once per link ever created.
        app.state.link_supervisors.pop(row.id, None)
        return
    supervisor, _ = _runtime(app, session, row.site_id)
    try:
        supervisor.forget(row.id)
    except Exception:
        pass


def _reconcile_remote(app: FastAPI, server_ids: list[str]) -> None:
    """Bring remote instances back after a control-plane restart.

    Best effort by design: a site that is down is skipped, not fatal, and the
    user sees the real reason on the instance card.
    """
    for server_id in server_ids:
        try:
            with Session(engine) as session:
                row = session.get(ServerRow, server_id)
                if row is None or not row.enabled or row.is_link:
                    continue
                _launch(app, session, row)
        except Exception:
            continue


def _purge_modbus_tags(session: Session) -> None:
    """Drop tag rows left over from the tag-based Modbus model.

    Modbus servers now expose only their four raw data areas; tags are an S7 /
    OPC UA concept. Idempotent, so it is safe on every startup.
    """
    server_ids = list(
        session.exec(
            select(ServerRow.id).where(ServerRow.protocol == Protocol.MODBUS_TCP.value)
        ).all()
    )
    if not server_ids:
        return
    session.exec(delete(TagRow).where(TagRow.server_id.in_(server_ids)))
    session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    # create_all() only makes missing tables; columns added since an older
    # build are applied here.
    ensure_columns(engine)
    supervisor = Supervisor(data_dir(), ROOT)
    app.state.supervisor = supervisor
    # One SSH-backed supervisor per remote site, plus a short memory of which
    # sites are currently unreachable.
    app.state.remote_supervisors = {}
    app.state.remote_errors = {}
    # One live Modbus connection per link instance.
    app.state.link_supervisors = {}
    # Modbus instances are raw-area only now: their address space is whatever
    # the client defines, so any tag rows left over from the old model must go.
    # S7 / OPC UA still need their tag tables and are untouched.
    with Session(engine) as session:
        _purge_modbus_tags(session)
    # Reconcile: bring back everything that was enabled before a restart.
    # Local instances come up inline; remote ones go to a thread because each
    # site costs at least one SSH round trip and may simply be down.
    with Session(engine) as session:
        rows = session.exec(select(ServerRow).where(ServerRow.enabled == True)).all()  # noqa: E712
        sites = {site.id: site for site in session.exec(select(SiteRow)).all()}
        pending = []
        local_keep: set[str] = set()
        for row in rows:
            # A link is one TCP connection, not a process: it comes back inline
            # because there is no machine to wait for.
            if row.is_link:
                try:
                    _launch(app, session, row)
                except Exception:
                    continue
                continue
            site = sites.get(row.site_id)
            if site is None or not site.is_local:
                pending.append(row.id)
                continue
            local_keep.add(row.id)
            try:
                # Not "spawn": a runtime left by the previous control plane is
                # adopted here when its config and identity still match.
                supervisor.start(_to_config(session, row))
            except Exception:
                continue
        # Anything this data dir owned but the database no longer enables (or
        # no longer has, or moved to a remote site) must stop holding a port.
        supervisor.reap_unwanted(local_keep)
    if pending:
        threading.Thread(
            target=_reconcile_remote, args=(app, pending), name="remote-reconcile", daemon=True
        ).start()
    try:
        yield
    finally:
        # The control plane is not the data plane: leave runtimes serving PLC
        # clients unless the operator explicitly opted into a full stop.
        supervisor.shutdown(
            stop_instances=os.environ.get("POLY_STOP_RUNTIMES_ON_EXIT") == "1"
        )
        SSH_POOL.close_all()


app = FastAPI(title="Poly PLC Simulator", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------- helpers
def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _audit(session: Session, action: str, target: str, detail: str = "") -> None:
    session.add(AuditRow(action=action, target=target, detail=detail[:4000]))


def _site(session: Session, site_id: str) -> SiteRow:
    row = session.get(SiteRow, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail="site not found")
    return row


def _site_access(row: SiteRow) -> dict[str, Any]:
    """Machine access info for the UI.

    The stored password is deliberately absent: the UI only ever needs to know
    whether one is configured, and a "read it back" endpoint is how secrets
    leak into logs and screenshots.
    """
    return {
        "host": row.host,
        "ssh_port": row.ssh_port,
        "ssh_user": row.ssh_user,
        "ssh_configured": secret_store.has_secret(row.ssh_secret),
        "deploy_dir": row.deploy_dir,
        "python_bin": row.python_bin,
        "local": row.is_local,
        "last_probe_at": row.last_probe_at,
        "last_probe": row.last_probe,
    }


def _server(session: Session, server_id: str) -> ServerRow:
    row = session.get(ServerRow, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="server not found")
    return row


def _tag_rows(session: Session, server_id: str) -> list[TagRow]:
    return list(
        session.exec(select(TagRow).where(TagRow.server_id == server_id).order_by(TagRow.position)).all()
    )


def _tag_def(row: TagRow) -> TagDef:
    return TagDef(
        name=row.name,
        data_type=DataType(row.data_type),
        area=row.area,
        address=row.address,
        bit=row.bit,
        db_number=row.db_number,
        word_order=WordOrder(row.word_order),
        writable=row.writable,
        description=row.description,
        sim=SimSpec(**row.sim) if row.sim else SimSpec(),
        node_id=row.node_id,
    )


def _to_config(session: Session, row: ServerRow) -> ServerConfig:
    # Modbus is raw-area only: even a stale tag row must not reach runtime.
    tags = (
        []
        if row.protocol == Protocol.MODBUS_TCP.value
        else [_tag_def(tag) for tag in _tag_rows(session, row.id)]
    )
    return ServerConfig(
        id=row.id,
        site_id=row.site_id,
        name=row.name,
        protocol=Protocol(row.protocol),
        bind_ip=row.bind_ip,
        port=row.port,
        options=ServerOptions(**row.options) if row.options else ServerOptions(),
        tags=tags,
        control_port=row.control_port,
    )


def _port_conflict(
    session: Session, site: SiteRow, port: int, exclude: str | None = None
) -> str | None:
    """Ports clash per *machine*, not per site.

    Two local sites are the same kernel socket table, so they still fight; two
    remote sites are two different boxes and never do.
    """
    # Links do not bind anything: their `port` is the target's port and lives
    # on somebody else's machine. Only sim instances can clash here.
    rows = session.exec(
        select(ServerRow).where(ServerRow.port == port, ServerRow.kind == "sim")
    ).all()
    for row in rows:
        if exclude and row.id == exclude:
            continue
        other = session.get(SiteRow, row.site_id)
        if other is None:
            continue
        same_machine = (
            other.id == site.id
            if not site.is_local
            else (site.id == other.id or other.is_local)
        )
        if same_machine:
            return f"端口 {port} 已被实例「{row.name}」占用（site「{other.name}」）"
    return None


def _bind_ip_error(bind_ip: str) -> str | None:
    if bind_ip in ("0.0.0.0", "::"):
        return None
    if bind_ip not in system_info().addresses:
        return f"{bind_ip} 不是本机地址"
    return None


def _local_bind_error(bind_ip: str, port: int) -> str | None:
    """Try the exact bind the runtime will do.

    The socket options have to match the listener's or the answer is wrong in
    one direction or the other: a bare bind is stricter than a real POSIX
    listener (it refuses TIME_WAIT) and looser than an exclusive Windows one
    (it accepts a port somebody is already listening on). `can_bind` uses the
    same options the adapters do - see `poly_core.net`.
    """
    return can_bind(bind_ip, port)


def _remote_port_check(
    app: FastAPI, site: SiteRow, port: int
) -> tuple[bool | None, str | None]:
    """(in_use, error). `error` means the check itself could not run."""
    try:
        return _remote_supervisor(app, site).port_in_use(port), None
    except Exception as exc:
        return None, str(exc)


def _assert_port_available(
    app: FastAPI,
    session: Session,
    site: SiteRow,
    bind_ip: str,
    port: int,
    exclude: str | None = None,
) -> None:
    """Reject a port that the database or the live machine already has.

    `exclude` relaxes only the *database* half (this instance's own row). It
    cannot relax the live half: a running instance really does hold the socket,
    and the probe cannot tell whose socket it is. That asymmetry is why the
    update path only calls this when the port actually changed - otherwise a
    rename would collide with the instance's own listener.
    """
    conflict = _port_conflict(session, site, port, exclude=exclude)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    if site.is_local:
        ip_error = _bind_ip_error(bind_ip)
        if ip_error:
            raise HTTPException(status_code=400, detail=ip_error)
        problem = _local_bind_error(bind_ip, port)
        if problem:
            raise HTTPException(
                status_code=409,
                detail=f"端口 {port} 在本机已被占用（{problem}）",
            )
        return
    busy, problem = _remote_port_check(app, site, port)
    if problem is not None:
        raise HTTPException(
            status_code=400,
            detail=f"无法在 {site.host} 上检查端口 {port}：{problem}",
        )
    if busy:
        raise HTTPException(status_code=409, detail=f"端口 {port} 在 {site.host} 上已被占用")


def _pick_control_port(session: Session, site: SiteRow) -> int:
    """A free loopback port on the target for the runtime's control API."""
    used = {row.control_port for row in session.exec(select(ServerRow)).all()}
    probe = _probe_of(site)
    if probe:
        used |= set(probe.ports_in_use)
    port = 17100
    while port in used:
        port += 1
    return port


def _check_bind_ip(bind_ip: str) -> None:
    if bind_ip == "0.0.0.0":
        return
    if bind_ip not in system_info().addresses:
        raise HTTPException(status_code=400, detail=f"{bind_ip} 不是本机地址")


def _server_payload(session: Session, row: ServerRow, state: dict[str, Any] | None) -> dict[str, Any]:
    tags = _tag_rows(session, row.id)
    payload = {
        "id": row.id,
        "site_id": row.site_id,
        "name": row.name,
        "kind": row.kind,
        "protocol": row.protocol,
        "bind_ip": row.bind_ip,
        "port": row.port,
        "runtime": row.runtime,
        "enabled": row.enabled,
        "options": row.options,
        "config_version": row.config_version,
        "created_at": row.created_at.isoformat(),
        "status": (state or {}).get("status", "stopped"),
        "error": (state or {}).get("error"),
        "endpoint": (state or {}).get("endpoint"),
        "clients": (state or {}).get("clients", 0),
        "started_at": (state or {}).get("started_at"),
        "latency_ms": (state or {}).get("latency_ms"),
        "tags": [
            {
                "id": tag.id,
                "position": tag.position,
                "name": tag.name,
                "data_type": tag.data_type,
                "area": tag.area,
                "address": tag.address,
                "bit": tag.bit,
                "db_number": tag.db_number,
                "word_order": tag.word_order,
                "writable": tag.writable,
                "description": tag.description,
                "sim": tag.sim,
                "node_id": tag.node_id,
            }
            for tag in tags
        ],
    }
    if row.is_link:
        # A link instance is a client, so "where it listens" is meaningless and
        # "where it connects" is the only address that matters.
        payload["target_host"] = row.target_host
        payload["target_port"] = row.target_port or row.port
        payload["target_options"] = row.target_options or LinkOptions().model_dump()
    return payload


def _default_tags(protocol: Protocol) -> list[TagDef]:
    """Seed demo tags for protocols that have a tag-based address space.

    Modbus deliberately gets none: the data area itself is the address space
    and clients define what each register means.
    """
    if protocol is Protocol.MODBUS_TCP:
        return []
    if protocol is Protocol.S7:
        return [
            TagDef(name="temperature", data_type=DataType.FLOAT32, area="db", db_number=1, address=0,
                   sim=SimSpec(mode=SimMode.SINE, min=10, max=35, period_ms=20000)),
            TagDef(name="pressure", data_type=DataType.FLOAT32, area="db", db_number=1, address=4,
                   sim=SimSpec(mode=SimMode.RANDOM, min=100, max=120)),
            TagDef(name="counter", data_type=DataType.INT16, area="db", db_number=1, address=8,
                   sim=SimSpec(mode=SimMode.RAMP, min=0, max=1000, step=1)),
            TagDef(name="running", data_type=DataType.BOOL, area="m", address=0, bit=0,
                   sim=SimSpec(mode=SimMode.TOGGLE, period_ms=6000)),
        ]
    return [
        TagDef(name="temperature", data_type=DataType.FLOAT32, area="opcua", address=0,
               sim=SimSpec(mode=SimMode.SINE, min=10, max=35, period_ms=20000)),
        TagDef(name="pressure", data_type=DataType.FLOAT32, area="opcua", address=0,
               sim=SimSpec(mode=SimMode.RANDOM, min=100, max=120)),
        TagDef(name="counter", data_type=DataType.UINT16, area="opcua", address=0,
               sim=SimSpec(mode=SimMode.RAMP, min=0, max=1000, step=1)),
        TagDef(name="running", data_type=DataType.BOOL, area="opcua", address=0,
               sim=SimSpec(mode=SimMode.TOGGLE, period_ms=6000)),
    ]


# --------------------------------------------------------------------- system
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": datetime.utcnow().isoformat()}


@app.get("/api/system")
def api_system(session: Session = Depends(get_session)) -> dict[str, Any]:
    info = system_info()
    # Ports are machine-global, not per site: two local sites on the same box
    # still fight over the same kernel socket table. Links bind nothing, and a
    # remote sim's port lives on the target machine, so neither belongs in this
    # machine's "used" list.
    local_sites = {site.id for site in session.exec(select(SiteRow)).all() if site.is_local}
    used = sorted(
        {
            row.port
            for row in session.exec(select(ServerRow).where(ServerRow.kind == "sim")).all()
            if row.site_id in local_sites
        }
    )
    return {
        **info.model_dump(),
        "data_dir": str(data_dir()),
        "addresses": ["0.0.0.0", *info.addresses],
        "used_ports": used,
    }


# --------------------------------------------------------------------- sites
@app.get("/api/sites")
def list_sites(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    sites = session.exec(select(SiteRow).order_by(SiteRow.created_at)).all()
    result = []
    for site in sites:
        servers = session.exec(select(ServerRow).where(ServerRow.site_id == site.id)).all()
        protocol_counts = {protocol.value: 0 for protocol in Protocol}
        for server in servers:
            if server.protocol in protocol_counts:
                protocol_counts[server.protocol] += 1
        result.append(
            {
                "id": site.id,
                "name": site.name,
                "description": site.description,
                "created_at": site.created_at.isoformat(),
                "server_count": len(servers),
                "protocol_counts": protocol_counts,
                "deploy": _site_deploy(site),
                **_site_access(site),
            }
        )
    return result


@app.post("/api/sites", status_code=201)
def create_site(body: SiteCreate, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = SiteRow(
        id=_new_id(),
        name=body.name.strip(),
        description=body.description,
        host=body.host.strip(),
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user.strip(),
        ssh_secret=secret_store.encrypt_secret(data_dir(), body.ssh_password),
        deploy_dir=body.deploy_dir or DEFAULT_DEPLOY_DIR,
    )
    session.add(row)
    _audit(session, "site.create", row.id, f"{row.name} {row.host or '本机'}")
    session.commit()
    return {**_site_payload_base(row)}


@app.patch("/api/sites/{site_id}")
def update_site(
    site_id: str,
    body: SiteUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _site(session, site_id)
    if body.name is not None:
        row.name = body.name.strip()
    if body.description is not None:
        row.description = body.description
    credentials_changed = False
    if body.host is not None:
        if body.host.strip() != row.host:
            credentials_changed = True
        row.host = body.host.strip()
    if body.ssh_port is not None:
        row.ssh_port = body.ssh_port
    if body.ssh_user is not None:
        row.ssh_user = body.ssh_user.strip()
    if body.deploy_dir:
        row.deploy_dir = body.deploy_dir
    # An empty password means "keep the stored one"; only a real value replaces it.
    if body.ssh_password:
        row.ssh_secret = secret_store.encrypt_secret(data_dir(), body.ssh_password)
        credentials_changed = True
    if credentials_changed or body.deploy_dir:
        # The cached supervisor is bound to the old target/path; a fresh one
        # will reconnect lazily on the next request.
        SSH_POOL.drop(row.id)
        request.app.state.remote_supervisors.pop(row.id, None)
        request.app.state.remote_errors.pop(row.id, None)
    _audit(session, "site.update", site_id, "")
    session.add(row)
    session.commit()
    return {**_site_payload_base(row)}


def _probe_access(
    host: str, ssh_port: int, ssh_user: str, password: str, deploy_dir: str
) -> SiteProbe:
    """Reach a machine once and report what it can run."""
    if not host.strip():
        return probe_local(deploy_dir)
    if not ssh_user:
        return SiteProbe(
            ok=False,
            error="还没有填 SSH 用户名",
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
    try:
        return run_probe(
            SshTarget(host=host, port=ssh_port, user=ssh_user, password=password),
            deploy_dir,
        )
    except SshError as exc:
        return SiteProbe(
            ok=False,
            error=str(exc),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )


@app.post("/api/sites/probe")
def probe_access(body: SiteAccessProbe) -> dict[str, Any]:
    """Test credentials for a machine that has no site yet.

    This is what the "create site" dialog calls: the user should find out
    whether a machine is reachable *before* it becomes a site. Nothing is
    stored.
    """
    probe = _probe_access(
        body.host, body.ssh_port, body.ssh_user, body.ssh_password, body.deploy_dir
    )
    return {"probe": probe.model_dump()}


@app.post("/api/sites/{site_id}/probe")
def probe_site(
    site_id: str,
    body: SiteProbeRequest | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Re-probe a stored site.

    `password` is optional and only used for this one connection, so a freshly
    typed credential can be tested before deciding to save it.
    """
    row = _site(session, site_id)
    probe = _probe_access(
        row.host,
        row.ssh_port,
        row.ssh_user,
        (body.password if body else None) or secret_store.decrypt_secret(data_dir(), row.ssh_secret),
        row.deploy_dir,
    )

    row.last_probe = probe.model_dump()
    row.last_probe_at = probe.checked_at
    if probe.python_bin:
        row.python_bin = probe.python_bin
    session.add(row)
    _audit(session, "site.probe", site_id, "ok" if probe.ok else (probe.error or ""))
    session.commit()
    return {
        "id": row.id,
        "probe": probe.model_dump(),
        "deploy": _site_deploy(row),
        **_site_access(row),
    }


def _site_payload_base(row: SiteRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at.isoformat(),
        "deploy": _site_deploy(row),
        **_site_access(row),
    }


@app.delete("/api/sites/{site_id}", status_code=204)
def delete_site(
    site_id: str, request: Request, session: Session = Depends(get_session)
) -> None:
    site = _site(session, site_id)
    remote = not site.is_local
    for row in session.exec(select(ServerRow).where(ServerRow.site_id == site_id)).all():
        if row.is_link:
            # A link owns a socket, not a process: forget() closes it.
            _forget(request.app, session, row)
        elif remote:
            _forget(request.app, session, row)
        else:
            request.app.state.supervisor.stop(row.id)
        session.exec(delete(TagRow).where(TagRow.server_id == row.id))
        session.delete(row)
    session.delete(session.get(SiteRow, site_id))
    SSH_POOL.drop(site_id)
    request.app.state.remote_supervisors.pop(site_id, None)
    _audit(session, "site.delete", site_id, "")
    session.commit()


# --------------------------------------------------------------------- servers
@app.get("/api/sites/{site_id}/servers")
def list_servers(
    site_id: str, request: Request, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    site = _site(session, site_id)
    rows = session.exec(select(ServerRow).where(ServerRow.site_id == site_id)).all()
    # A remote site can be unreachable; one failed handshake must not turn the
    # whole page into a 500, so it becomes a per-site error on every row.
    return [_server_payload(session, row, _safe_state(request.app, session, row)) for row in rows]


@app.get("/api/servers")
def list_all_servers(
    request: Request, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Every instance, with the site it belongs to.

    The overview page needs one list across machines; making the browser ask
    each site separately would turn one page into N round trips (and N SSH
    handshakes on the first load). Status still comes from each instance's own
    supervisor, but only once per row.
    """
    sites = {site.id: site for site in session.exec(select(SiteRow)).all()}
    rows = session.exec(select(ServerRow).order_by(ServerRow.created_at)).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        site = sites.get(row.site_id)
        payload = _server_payload(session, row, _safe_state(request.app, session, row))
        payload["site_name"] = site.name if site else ""
        payload["site_host"] = site.host if site else ""
        payload["site_local"] = bool(site and site.is_local)
        result.append(payload)
    return result


def _probe_link_target(target: LinkTarget) -> dict[str, Any]:
    """Reach somebody else's server once and report whether it answers.

    Nothing is stored and nothing is cached: this is the "测试连接" button, and
    it must tell the truth about the address *right now*, not about the last
    time we happened to look.

    A device that answers and refuses is *alive*. It spoke the protocol, it
    merely did not like the probe. Treating that as "连不上" would block a
    perfectly usable link - and would disagree with `LinkSupervisor._probe`,
    which deliberately keeps such a link green.
    """
    client = build_link_client(target.protocol)
    client.configure(target)
    started = time.monotonic()
    try:
        latency = client.probe()
    except LinkDeviceError as exc:
        return {
            "ok": True,
            "error": None,
            "warning": str(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "endpoint": target.endpoint,
            "namespaces": client.namespaces,
        }
    except (LinkError, OSError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "warning": None,
            "latency_ms": None,
            "endpoint": target.endpoint,
            "namespaces": [],
        }
    finally:
        # `close()` is the *disable* half of a link's lifecycle: it drops the
        # session but deliberately leaves the OPC UA event loop running so the
        # same client can be re-enabled. A probe is a throwaway client with no
        # instance behind it, so leaving the loop behind leaks one thread per
        # click. `dispose()` is the deletion half, and this object is deleted
        # the moment the function returns.
        client.dispose()
    return {
        "ok": True,
        "error": None,
        "warning": None,
        "latency_ms": latency,
        "endpoint": target.endpoint,
        # OPC UA only: the far server's namespace table. The index a tag should
        # use cannot be guessed, and this is where the user finds it.
        "namespaces": client.namespaces,
    }


@app.post("/api/sites/{site_id}/link-test")
def link_test(
    site_id: str,
    body: LinkOptions,
    host: str = Query(..., min_length=1),
    port: int = Query(default=502, ge=1, le=65535),
    protocol: Protocol = Query(default=Protocol.MODBUS_TCP),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Test a link target before it becomes an instance."""
    _site(session, site_id)
    return _probe_link_target(
        LinkTarget(
            host=host.strip(),
            port=port,
            protocol=protocol,
            unit_id=body.unit_id,
            rack=body.rack,
            slot=body.slot,
            timeout=body.timeout_ms / 1000.0,
            namespace_index=body.namespace_index,
        )
    )


@app.get("/api/sites/{site_id}/port-check")
def port_check(
    site_id: str,
    request: Request,
    port: int = Query(..., ge=1, le=65535),
    bind_ip: str = Query(default="0.0.0.0"),
    exclude: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Would an instance be able to listen on this port right now?

    A query, not an action: an occupied port answers 200 with
    ``available: false`` and the reason, so the dialog can say so while the
    user is still typing. It runs the exact check ``create_server`` will run,
    so the pre-flight and the save can never disagree.
    """
    site = _site(session, site_id)
    try:
        _assert_port_available(request.app, session, site, bind_ip, port, exclude=exclude)
    except HTTPException as exc:
        return {"port": port, "bind_ip": bind_ip, "available": False, "reason": str(exc.detail)}
    return {"port": port, "bind_ip": bind_ip, "available": True, "reason": None}


@app.post("/api/sites/{site_id}/servers", status_code=201)
def create_server(
    site_id: str,
    body: ServerCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    site = _site(session, site_id)
    link = body.kind == "link"
    remote = not site.is_local
    if link:
        if body.protocol not in (Protocol.MODBUS_TCP, Protocol.S7, Protocol.OPCUA):
            raise HTTPException(
                status_code=400,
                detail=f"连接实例暂不支持 {body.protocol.value}",
            )
        if not body.target_host.strip():
            raise HTTPException(status_code=400, detail="连接实例必须填目标地址")
        # A link is a client: nothing of ours listens anywhere, so the whole
        # bind / port / protocol-availability side of a sim instance is skipped.
        # The one thing that matters is that the target actually answers - a
        # saved link that cannot connect is a card that is red from birth.
        probe = _probe_link_target(_link_target_from_body(body))
        if not probe["ok"]:
            raise HTTPException(
                status_code=400,
                detail=f"连不上 {body.target_host.strip()}:{body.target_port}：{probe['error']}",
            )
    else:
        if remote:
            # The instance listens on the *target's* interfaces; this box's NICs
            # are meaningless there, and protocol support comes from what we
            # install, not from what this machine happens to import.
            bind_ip = "0.0.0.0"
        else:
            bind_ip = body.bind_ip
            info = system_info()
            if body.protocol.value not in info.protocols:
                raise HTTPException(status_code=400, detail=f"本机不支持协议 {body.protocol.value}")
        # Any number of instances may share a site, including several of the
        # same protocol: each one is its own process with its own data plane,
        # port and control socket. The only thing that may not repeat is the
        # port, and that is what the next call checks.
        _assert_port_available(request.app, session, site, bind_ip, body.port)

    if body.protocol is Protocol.MODBUS_TCP and body.tags:
        raise HTTPException(
            status_code=400,
            detail="Modbus 实例只提供原始数据区，不使用标签表；请连接后用客户端自行定义地址",
        )
    tags = [] if body.protocol is Protocol.MODBUS_TCP else list(body.tags)
    if not tags and body.sample_tags and body.protocol is not Protocol.MODBUS_TCP:
        tags = _default_tags(body.protocol)
    errors = validate_tags(body.protocol, tags)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    row = ServerRow(
        id=_new_id(),
        site_id=site_id,
        name=body.name.strip(),
        kind=body.kind,
        protocol=body.protocol.value,
        bind_ip="0.0.0.0" if link else bind_ip,
        # A link's port is the target's port: one number on the card, and it
        # happens to be the one thing about the link worth showing.
        port=body.target_port if link else body.port,
        options=body.options.model_dump(),
        # Remote runtimes are reachable only through the SSH channel, so their
        # control port has to be known in advance and stay put.
        control_port=0 if not remote else _pick_control_port(session, site),
        target_host=body.target_host.strip() if link else "",
        target_port=body.target_port if link else 0,
        target_options=body.target_options.model_dump() if link else {},
    )
    session.add(row)
    for position, tag in enumerate(tags):
        session.add(_tag_row(row.id, position, tag))
    _audit(
        session,
        "server.create",
        row.id,
        (
            f"link {row.target_host}:{row.target_port}"
            if link
            else f"{row.protocol} {row.bind_ip}:{row.port}" + (f" @{site.host}" if remote else "")
        ),
    )
    session.commit()

    state = _launch(request.app, session, row)
    return _server_payload(session, row, state)


@app.get("/api/servers/{server_id}")
def get_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    return _server_payload(session, row, _safe_state(request.app, session, row))


@app.patch("/api/servers/{server_id}")
def update_server(
    server_id: str,
    body: ServerUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    site = _site(session, row.site_id)
    link = row.is_link
    # Captured before the assignment below rewrites it. An unchanged port is
    # held by this very instance, so it may only be checked against the
    # database: a live bind test would refuse the instance's own socket and
    # turn every rename into a 409.
    port_changed = body.port is not None and body.port != row.port
    if not link and body.bind_ip is not None and site.is_local:
        # A remote instance binds on its own machine; forcing 0.0.0.0 here
        # would silently ignore the user, so the field is simply not editable.
        _check_bind_ip(body.bind_ip)
        row.bind_ip = body.bind_ip
    if body.target_host is not None:
        if link and not body.target_host.strip():
            raise HTTPException(status_code=400, detail="目标地址不能为空")
        row.target_host = body.target_host.strip()
    if body.target_port is not None:
        row.target_port = body.target_port
    if body.target_options is not None:
        row.target_options = body.target_options.model_dump()
    if link:
        # A client's "port" is the target's port, kept in sync so the card,
        # the subtitle and the connection all say the same number.
        row.port = row.target_port or row.port
        # Only a changed target has to answer. Renaming or disabling a link is
        # exactly what a user does when the far side is down, so neither may be
        # held hostage by a probe.
        target_changed = (
            body.target_host is not None
            or body.target_port is not None
            or body.target_options is not None
        )
        if target_changed and body.enabled is not False:
            probe = _probe_link_target(_link_target(row))
            if not probe["ok"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"连不上 {row.target_host}:{row.target_port or row.port}：{probe['error']}",
                )
    if body.port is not None and not link:
        row.port = body.port
    if body.name is not None:
        row.name = body.name.strip()
    if body.options is not None:
        row.options = body.options.model_dump()
    if body.enabled is not None:
        row.enabled = body.enabled
    if not link:
        if port_changed:
            # A new port has to be free everywhere the old one was: the
            # database, this machine's socket table, or the target's.
            _assert_port_available(
                request.app, session, site, row.bind_ip, row.port, exclude=row.id
            )
        else:
            conflict = _port_conflict(session, site, row.port, exclude=row.id)
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)

    row.config_version += 1
    session.add(row)
    _audit(session, "server.update", row.id, f"v{row.config_version}")
    session.commit()

    # Disabling is a desired state, not a command: stop the process (sim) or
    # drop the socket (link), and leave it stopped until somebody enables the
    # instance again. One branch covers both kinds because `row.enabled` has
    # already been written above.
    state: dict[str, Any] = {"status": "stopped"}
    if row.enabled:
        state = _launch(request.app, session, row, restart=True)
    else:
        _stop(request.app, session, row)
    return _server_payload(session, row, state)


@app.delete("/api/servers/{server_id}", status_code=204)
def delete_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> None:
    row = _server(session, server_id)
    _forget(request.app, session, row)
    session.exec(delete(TagRow).where(TagRow.server_id == server_id))
    session.delete(row)
    _audit(session, "server.delete", server_id, "")
    session.commit()


@app.post("/api/servers/{server_id}/restart")
def restart_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    if not row.enabled:
        # Restart is the one explicit command, but it must not override the
        # desired state. A disabled instance is started only by enabling it.
        raise HTTPException(status_code=409, detail="实例已停用，请先启用再重启")
    return _server_payload(session, row, _launch(request.app, session, row, restart=True))


# --------------------------------------------------------------------- tags
@app.put("/api/servers/{server_id}/tags")
def replace_tags(
    server_id: str,
    tags: list[TagDef],
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    if row.protocol == Protocol.MODBUS_TCP.value:
        raise HTTPException(
            status_code=400,
            detail="Modbus 实例只提供原始数据区，不使用标签表",
        )
    errors = validate_tags(Protocol(row.protocol), tags)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    session.exec(delete(TagRow).where(TagRow.server_id == server_id))
    for position, tag in enumerate(tags):
        session.add(_tag_row(server_id, position, tag))
    row.config_version += 1
    session.add(row)
    _audit(session, "tags.replace", server_id, f"{len(tags)} tags, v{row.config_version}")
    session.commit()

    state: dict[str, Any] = {"status": "stopped"}
    if row.enabled:
        state = _launch(request.app, session, row, restart=True)
    return {**_server_payload(session, row, state)}


@app.get("/api/servers/{server_id}/values")
def server_values(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    if not row.enabled:
        return {
            "status": "stopped",
            "values": {},
            "clients": 0,
            "error": None,
            "endpoint": None,
            "started_at": None,
            "latency_ms": None,
        }
    if row.is_link:
        return _link_supervisor(request.app, session, row).values().model_dump(mode="json")
    supervisor, _ = _runtime(request.app, session, row.site_id)
    return supervisor.values(server_id).model_dump(mode="json")


def _area_limit(row: ServerRow) -> int:
    """How many points each area exposes, counting from address 0.

    A link's map belongs to the far device, so we do not claim to know it: the
    request goes out and the device answers (or refuses with an exception).
    """
    if row.is_link:
        return 65536
    return int(row.options.get("register_count") or 65536)


@app.get("/api/servers/{server_id}/area")
def server_area(
    server_id: str,
    request: Request,
    area: str = Query(default="holding"),
    address: int = Query(default=0, ge=0, le=65535),
    count: int = Query(default=50, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Raw Modbus data-plane read, independent of the tag table.

    This is what the register viewer uses: a real client may read any address
    in 0..65535, so the UI must be able to show the same range - up to the
    instance's own configured map, if it has one.
    """
    row = _server(session, server_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用，请先启用后再读取数据区")
    if row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="数据区浏览目前只支持 Modbus TCP")
    if area not in ("coil", "discrete", "holding", "input"):
        raise HTTPException(status_code=400, detail=f"未知数据区: {area}")
    limit = _area_limit(row)
    if address + count > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"本实例只暴露 {limit} 个地址（0–{limit - 1}）；"
                "要看整段 0–65535，把实例的「有效寄存器数」留空"
            ),
        )
    options = LinkOptions(**row.target_options) if row.is_link else LinkOptions(
        unit_id=int(row.options.get("unit_id", 1))
    )
    try:
        if row.is_link:
            values = _link_supervisor(request.app, session, row).read_area(area, address, count)
        else:
            supervisor, _ = _runtime(request.app, session, row.site_id)
            values = supervisor.read_area(server_id, area, address, count)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "server_id": server_id,
        "area": area,
        "address": address,
        "count": count,
        "unit_id": options.unit_id,
        # The map's size, so the page can say "有效 0–99" and refuse a range
        # before asking. A link reports the full space: the device decides.
        "limit": limit,
        "values": values,
    }


@app.post("/api/servers/{server_id}/area")
def write_server_area(
    server_id: str,
    body: AreaWriteRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Write one raw Modbus register/coil from the admin UI.

    This is intentionally separate from `/write` (tag based): Modbus instances
    have no tag table, and input/discrete areas are read-only for real clients,
    so the simulator UI is the only way to preset them. On a *link* instance the
    write lands on somebody else's machine - see the confirm box in the UI.
    """
    row = _server(session, server_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用，请先启用后再写入数据区")
    if row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="数据区写入目前只支持 Modbus TCP")
    if body.area not in ("coil", "discrete", "holding", "input"):
        raise HTTPException(status_code=400, detail=f"未知数据区: {body.area}")
    limit = _area_limit(row)
    if body.address >= limit:
        raise HTTPException(
            status_code=400,
            detail=f"地址 {body.address} 超出本实例的有效范围（0–{limit - 1}）",
        )
    try:
        if row.is_link:
            _link_supervisor(request.app, session, row).write_area(body.area, body.address, body.value)
        else:
            supervisor, _ = _runtime(request.app, session, row.site_id)
            supervisor.write_area(server_id, body.area, body.address, body.value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        session,
        "area.write",
        f"{server_id}/{body.area}:{body.address}",
        str(body.value) + (" (link)" if row.is_link else ""),
    )
    session.commit()
    return {
        "ok": True,
        "area": body.area,
        "address": body.address,
        "value": body.value,
    }


@app.post("/api/servers/{server_id}/write")
def write_tag(
    server_id: str,
    body: WriteRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="实例已停用，请先启用后再写入")
    try:
        if row.is_link:
            # No tag table on the far side either: the connection has no idea
            # what a name means, only addresses do.
            _link_supervisor(request.app, session, row).write(body.tag, body.value)
        else:
            supervisor, _ = _runtime(request.app, session, row.site_id)
            supervisor.write(server_id, body.tag, body.value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(session, "tag.write", f"{server_id}/{body.tag}", str(body.value))
    session.commit()
    return {"ok": True}


@app.get("/api/servers/{server_id}/logs")
def server_logs(
    server_id: str,
    request: Request,
    lines: int = Query(default=200, ge=10, le=2000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    if row.is_link:
        # A link has no process to tail: its "log" is the connection history.
        return {
            "server_id": server_id,
            "log": _link_supervisor(request.app, session, row).logs(lines),
            "link": True,
        }
    supervisor, _ = _runtime(request.app, session, row.site_id)
    return {"server_id": server_id, "log": supervisor.logs(server_id, lines), "link": False}


# --------------------------------------------------------------------- audit
@app.get("/api/audit")
def list_audit(
    limit: int = Query(default=50, ge=1, le=500), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    rows = session.exec(select(AuditRow).order_by(AuditRow.id.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "action": row.action,
            "target": row.target,
            "detail": row.detail,
        }
        for row in rows
    ]


# --------------------------------------------------------------------- static web
if DIST.exists():
    assets = DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        candidate = DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")


def _tag_row(server_id: str, position: int, tag: TagDef) -> TagRow:
    return TagRow(
        id=_new_id(),
        server_id=server_id,
        position=position,
        name=tag.name,
        data_type=tag.data_type.value,
        area=tag.area,
        address=tag.address,
        bit=tag.bit,
        db_number=tag.db_number,
        word_order=tag.word_order.value,
        writable=tag.writable,
        description=tag.description,
        sim=tag.sim.model_dump(),
        node_id=(tag.node_id or "").strip() or None,
    )
