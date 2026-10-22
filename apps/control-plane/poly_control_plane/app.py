from __future__ import annotations

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
from poly_core import validate_tags

from . import secret as secret_store
from .db import AuditRow, ServerRow, SiteRow, TagRow
from .migrate import ensure_columns
from .ssh import (
    DEFAULT_DEPLOY_DIR,
    SshError,
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


def supervisor_of(request: Request) -> Supervisor:
    return request.app.state.supervisor


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
    # Modbus instances are raw-area only now: their address space is whatever
    # the client defines, so any tag rows left over from the old model must go.
    # S7 / OPC UA still need their tag tables and are untouched.
    with Session(engine) as session:
        _purge_modbus_tags(session)
    # Reconcile: bring back everything that was enabled before a restart.
    with Session(engine) as session:
        rows = session.exec(select(ServerRow).where(ServerRow.enabled == True)).all()  # noqa: E712
        for row in rows:
            try:
                supervisor.start(_to_config(session, row))
            except Exception:
                continue
    try:
        yield
    finally:
        supervisor.shutdown()


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
    )


def _port_conflict(session: Session, bind_ip: str, port: int, exclude: str | None = None) -> str | None:
    rows = session.exec(select(ServerRow).where(ServerRow.port == port)).all()
    for row in rows:
        if exclude and row.id == exclude:
            continue
        if row.bind_ip == bind_ip or "0.0.0.0" in (row.bind_ip, bind_ip):
            site = session.exec(select(SiteRow).where(SiteRow.id == row.site_id)).first()
            site_name = site.name if site else row.site_id
            return f"端口 {port} 已被 site「{site_name}」的实例「{row.name}」占用"
    return None


def _check_bind_ip(bind_ip: str) -> None:
    if bind_ip == "0.0.0.0":
        return
    if bind_ip not in system_info().addresses:
        raise HTTPException(status_code=400, detail=f"{bind_ip} 不是本机地址")


def _state_of(request: Request, server_id: str) -> dict[str, Any]:
    supervisor: Supervisor = supervisor_of(request)
    return supervisor.state(server_id).model_dump(mode="json")


def _server_payload(session: Session, row: ServerRow, state: dict[str, Any] | None) -> dict[str, Any]:
    tags = _tag_rows(session, row.id)
    return {
        "id": row.id,
        "site_id": row.site_id,
        "name": row.name,
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
            }
            for tag in tags
        ],
    }


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
    # Ports are machine-global, not per site: two sites on the same box still
    # fight over the same kernel socket table. Surface the occupied ones so the
    # UI can suggest a free port instead of failing on bind.
    used = sorted({row.port for row in session.exec(select(ServerRow)).all()})
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
        result.append(
            {
                "id": site.id,
                "name": site.name,
                "description": site.description,
                "created_at": site.created_at.isoformat(),
                "server_count": len(servers),
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
def update_site(site_id: str, body: SiteUpdate, session: Session = Depends(get_session)) -> dict[str, Any]:
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
    if credentials_changed:
        SSH_POOL.drop(row.id)
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
    return {"id": row.id, "probe": probe.model_dump(), **_site_access(row)}


def _site_payload_base(row: SiteRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at.isoformat(),
        **_site_access(row),
    }


@app.delete("/api/sites/{site_id}", status_code=204)
def delete_site(
    site_id: str, request: Request, session: Session = Depends(get_session)
) -> None:
    _site(session, site_id)
    supervisor: Supervisor = supervisor_of(request)
    for row in session.exec(select(ServerRow).where(ServerRow.site_id == site_id)).all():
        supervisor.stop(row.id)
        session.exec(delete(TagRow).where(TagRow.server_id == row.id))
        session.delete(row)
    session.delete(session.get(SiteRow, site_id))
    _audit(session, "site.delete", site_id, "")
    session.commit()


# --------------------------------------------------------------------- servers
@app.get("/api/sites/{site_id}/servers")
def list_servers(
    site_id: str, request: Request, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    _site(session, site_id)
    supervisor: Supervisor = supervisor_of(request)
    states = supervisor.states()
    rows = session.exec(select(ServerRow).where(ServerRow.site_id == site_id)).all()
    return [
        _server_payload(
            session,
            row,
            states.get(row.id).model_dump(mode="json") if row.id in states else {"status": "stopped"},
        )
        for row in rows
    ]


@app.post("/api/sites/{site_id}/servers", status_code=201)
def create_server(
    site_id: str,
    body: ServerCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _site(session, site_id)
    info = system_info()
    if body.protocol.value not in info.protocols:
        raise HTTPException(status_code=400, detail=f"本机不支持协议 {body.protocol.value}")
    _check_bind_ip(body.bind_ip)
    conflict = _port_conflict(session, body.bind_ip, body.port)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

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
        protocol=body.protocol.value,
        bind_ip=body.bind_ip,
        port=body.port,
        options=body.options.model_dump(),
    )
    session.add(row)
    for position, tag in enumerate(tags):
        session.add(_tag_row(row.id, position, tag))
    _audit(session, "server.create", row.id, f"{row.protocol} {row.bind_ip}:{row.port}")
    session.commit()

    supervisor: Supervisor = supervisor_of(request)
    state: dict[str, Any] | None = None
    try:
        state = supervisor.start(_to_config(session, row)).model_dump(mode="json")
    except Exception as exc:
        state = {"status": "error", "error": str(exc)}
    return _server_payload(session, row, state)


@app.get("/api/servers/{server_id}")
def get_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    return _server_payload(session, row, _state_of(request, server_id))


@app.patch("/api/servers/{server_id}")
def update_server(
    server_id: str,
    body: ServerUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = _server(session, server_id)
    if body.bind_ip is not None:
        _check_bind_ip(body.bind_ip)
        row.bind_ip = body.bind_ip
    if body.port is not None:
        row.port = body.port
    if body.name is not None:
        row.name = body.name.strip()
    if body.options is not None:
        row.options = body.options.model_dump()
    if body.enabled is not None:
        row.enabled = body.enabled
    conflict = _port_conflict(session, row.bind_ip, row.port, exclude=row.id)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    row.config_version += 1
    session.add(row)
    _audit(session, "server.update", row.id, f"v{row.config_version}")
    session.commit()

    supervisor: Supervisor = supervisor_of(request)
    state: dict[str, Any] | None = {"status": "stopped"}
    if row.enabled:
        try:
            state = supervisor.restart(_to_config(session, row)).model_dump(mode="json")
        except Exception as exc:
            state = {"status": "error", "error": str(exc)}
    else:
        supervisor.stop(row.id)
    return _server_payload(session, row, state)


@app.delete("/api/servers/{server_id}", status_code=204)
def delete_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> None:
    row = _server(session, server_id)
    supervisor_of(request).stop(server_id)
    session.exec(delete(TagRow).where(TagRow.server_id == server_id))
    session.delete(row)
    _audit(session, "server.delete", server_id, "")
    session.commit()


@app.post("/api/servers/{server_id}/restart")
def restart_server(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    supervisor: Supervisor = supervisor_of(request)
    try:
        state = supervisor.restart(_to_config(session, row)).model_dump(mode="json")
    except Exception as exc:
        state = {"status": "error", "error": str(exc)}
    return _server_payload(session, row, state)


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

    supervisor: Supervisor = supervisor_of(request)
    state: dict[str, Any] | None = {"status": "stopped"}
    if row.enabled:
        try:
            state = supervisor.restart(_to_config(session, row)).model_dump(mode="json")
        except Exception as exc:
            state = {"status": "error", "error": str(exc)}
    return {**_server_payload(session, row, state)}


@app.get("/api/servers/{server_id}/values")
def server_values(
    server_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = _server(session, server_id)
    if not row.enabled:
        return {"status": "stopped", "values": {}, "clients": 0, "error": None}
    supervisor: Supervisor = supervisor_of(request)
    return supervisor.values(server_id).model_dump(mode="json")


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
    in 0..65535, so the UI must be able to show the same range.
    """
    row = _server(session, server_id)
    if row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="数据区浏览目前只支持 Modbus TCP")
    if area not in ("coil", "discrete", "holding", "input"):
        raise HTTPException(status_code=400, detail=f"未知数据区: {area}")
    if address + count > 65536:
        raise HTTPException(
            status_code=400, detail=f"读取范围越界: {address} + {count} > 65536"
        )
    supervisor: Supervisor = supervisor_of(request)
    try:
        values = supervisor.read_area(server_id, area, address, count)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "server_id": server_id,
        "area": area,
        "address": address,
        "count": count,
        "unit_id": int(row.options.get("unit_id", 1)),
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
    so the simulator UI is the only way to preset them.
    """
    row = _server(session, server_id)
    if row.protocol != "modbus_tcp":
        raise HTTPException(status_code=400, detail="数据区写入目前只支持 Modbus TCP")
    if body.area not in ("coil", "discrete", "holding", "input"):
        raise HTTPException(status_code=400, detail=f"未知数据区: {body.area}")
    supervisor: Supervisor = supervisor_of(request)
    try:
        supervisor.write_area(server_id, body.area, body.address, body.value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        session,
        "area.write",
        f"{server_id}/{body.area}:{body.address}",
        str(body.value),
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
    _server(session, server_id)
    supervisor: Supervisor = supervisor_of(request)
    try:
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
    _server(session, server_id)
    supervisor: Supervisor = supervisor_of(request)
    return {"server_id": server_id, "log": supervisor.logs(server_id, lines)}


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
    )
