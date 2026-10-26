"""Shared contracts between the control plane, the agent and the simulator runtime.

Everything in this module must stay free of protocol/runtime imports: it is the
only package both sides agree on.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")

MODBUS_AREAS = ("holding", "input", "coil", "discrete")
S7_AREAS = ("db", "m", "i", "q")


class Protocol(str, Enum):
    MODBUS_TCP = "modbus_tcp"
    S7 = "s7"
    OPCUA = "opcua"


class RuntimeDriver(str, Enum):
    NATIVE = "native"
    DOCKER = "docker"


class DataType(str, Enum):
    BOOL = "bool"
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    FLOAT32 = "float32"
    FLOAT64 = "float64"


class WordOrder(str, Enum):
    BIG = "big"
    LITTLE = "little"


class SimMode(str, Enum):
    STATIC = "static"
    SINE = "sine"
    RANDOM = "random"
    RAMP = "ramp"
    TOGGLE = "toggle"


class SimSpec(BaseModel):
    """Optional value generator so a tag can move without a real client."""

    mode: SimMode = SimMode.STATIC
    min: float = 0.0
    max: float = 100.0
    period_ms: int = Field(default=5000, ge=200, le=3_600_000)
    step: float = 1.0


class TagDef(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    data_type: DataType = DataType.FLOAT32
    area: str = "holding"
    address: int = Field(default=0, ge=0, le=65535)
    bit: int | None = Field(default=None, ge=0, le=7)
    db_number: int | None = Field(default=None, ge=1, le=65535)
    word_order: WordOrder = WordOrder.BIG
    writable: bool = True
    description: str = ""
    sim: SimSpec = Field(default_factory=SimSpec)
    # OPC UA only. A node is not addressed by "area + offset" but by a NodeId
    # such as `ns=2;s=temperature` or `ns=3;i=1001`, and the namespace index is
    # assigned by the *far* server - there is nothing predictable about it. A
    # tag may therefore carry the full NodeId; left empty it is derived from
    # the link's namespace index and the tag name, which is exactly how our own
    # OPC UA server names its variables, so linking to a simulator needs no
    # address at all.
    node_id: str | None = Field(default=None, max_length=256)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError(
                "name must start with a letter/underscore and contain only letters, digits, '_', '.', '-'"
            )
        return value


class ServerOptions(BaseModel):
    unit_id: int = Field(default=1, ge=1, le=247)
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    endpoint_url: str | None = None
    advertise_host: str | None = None
    namespace: str | None = None
    # Modbus only: how many points each of the four areas exposes, counting from
    # address 0. None means the whole 16-bit space (65536), which is what a
    # scanning client expects; a number makes the instance behave like a real
    # device that answers ILLEGAL_ADDRESS past its configured map.
    #
    # Applied to coils and discrete inputs as a count of bits - one number keeps
    # the configuration honest ("my device has 1000 points"), and a real device
    # with 1000 registers and 2000 coils is a thing nobody has asked for yet.
    register_count: int | None = Field(default=None, ge=1, le=65536)


class LinkOptions(BaseModel):
    """How to talk to a server somebody else is running.

    The platform opens one long-lived connection per link instance and every
    page read/write goes over it. `timeout_ms` is the per-request budget; a
    device that misses it is reported as unreachable, not as "no".

    `unit_id` is Modbus only; `rack`/`slot` are S7 only; `namespace_index` is
    OPC UA only. Keeping them all here lets the same link record be switched
    between protocols without a second options table.
    """

    unit_id: int = Field(default=1, ge=0, le=255)
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    timeout_ms: int = Field(default=3000, ge=200, le=60000)
    # OPC UA only. Which namespace the bare tag names live in. 2 is the value
    # our own server ends up on (0 is the OPC UA standard, 1 the server's own
    # application namespace) and is therefore the right default for linking to
    # a simulator; a third-party server usually needs its own index, which the
    # "测试连接" result lists by name.
    namespace_index: int = Field(default=2, ge=0, le=65535)


class ServerConfig(BaseModel):
    id: str
    site_id: str
    name: str
    protocol: Protocol
    bind_ip: str = "0.0.0.0"
    port: int = Field(ge=1, le=65535)
    options: ServerOptions = Field(default_factory=ServerOptions)
    tags: list[TagDef] = Field(default_factory=list)
    # Loopback control port of the runtime. 0 means "pick one":
    # locally that is an ephemeral port, remotely it is assigned by the
    # platform so it can be reached again through the SSH channel.
    control_port: int = 0


class RuntimeState(BaseModel):
    server_id: str
    status: Literal["starting", "running", "error", "stopped"] = "starting"
    started_at: float | None = None
    error: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    clients: int = 0
    endpoint: str | None = None
    # Round trip of the last successful exchange, in milliseconds. Only a link
    # instance has it - a local process has no network to measure.
    latency_ms: int | None = None


class SystemInfo(BaseModel):
    os: str
    arch: str
    python: str
    hostname: str
    addresses: list[str]
    protocols: list[str]
    protocol_notes: dict[str, str] = Field(default_factory=dict)


class SiteCreate(BaseModel):
    """A site is one target machine.

    An empty `host` means "this machine": those instances are supervised
    in-process, which is how the local build works today.
    """

    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    host: str = ""
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = ""
    ssh_password: str = ""
    deploy_dir: str = "~/.poly-sim"


class SiteUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    host: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = None
    # Empty string means "leave the stored password alone"; a value replaces it.
    ssh_password: str | None = None
    deploy_dir: str | None = None


class SiteProbe(BaseModel):
    """What one SSH round trip tells us about a target machine."""

    ok: bool
    error: str | None = None
    os: str = ""
    arch: str = ""
    # "Ubuntu 22.04.3 LTS" etc., from /etc/os-release. Empty when unreadable.
    distro: str = ""
    python: str = ""
    python_bin: str = ""
    python_candidates: list[str] = Field(default_factory=list)
    python_ok: bool = False
    package_manager: str = ""
    docker: str = ""
    # Whether the SSH user can actually talk to the daemon. A docker binary
    # everyone can see but nobody may drive is the common case on shared boxes.
    docker_ok: bool = False
    uv: str = ""
    deploy_dir: str = ""
    deploy_ok: bool = False
    ports_in_use: list[int] = Field(default_factory=list)
    checked_at: str = ""


class DeployPlan(BaseModel):
    """How the platform will run instances on one machine.

    The UI shows this, it never lets the user pick it: the machine decides what
    is possible, and the only thing a user can do about it is fix the machine
    (install Python, join the docker group) and come back.
    """

    carrier: str = ""  # native | uv | docker | unsupported
    label: str = ""
    reason: str = ""
    action: str = ""
    python: str = ""
    ready: bool = False


class SiteProbeRequest(BaseModel):
    """Optional password so a freshly typed one can be tested before saving."""

    password: str | None = None


class SiteAccessProbe(BaseModel):
    """Probe a machine that has no site yet - used while creating one."""

    host: str = ""
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = ""
    ssh_password: str = ""
    deploy_dir: str = "~/.poly-sim"


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 'sim' = we deploy and run the server; 'link' = we connect to one that is
    # already running somewhere else and read/write through it.
    kind: str = "sim"
    protocol: Protocol
    bind_ip: str = "0.0.0.0"
    port: int = Field(ge=1, le=65535)
    options: ServerOptions = Field(default_factory=ServerOptions)
    # Explicit tags win. With no tags, sample_tags=True seeds the demo table;
    # False creates an all-zero data plane, which is what most test clients want.
    tags: list[TagDef] = Field(default_factory=list)
    sample_tags: bool = True
    # Link only: where the other server lives.
    target_host: str = ""
    target_port: int = Field(default=502, ge=1, le=65535)
    target_options: LinkOptions = Field(default_factory=LinkOptions)


class ServerUpdate(BaseModel):
    name: str | None = None
    bind_ip: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    options: ServerOptions | None = None
    enabled: bool | None = None
    # Link only. Changing these re-points the connection on the next request.
    target_host: str | None = None
    target_port: int | None = Field(default=None, ge=1, le=65535)
    target_options: LinkOptions | None = None


class WriteRequest(BaseModel):
    tag: str
    value: Any

    model_config = {"arbitrary_types_allowed": True}


class AreaWriteRequest(BaseModel):
    """Raw data-plane write used by the UI when no tag table exists."""

    area: str
    address: int = Field(ge=0, le=65535)
    value: Any

    model_config = {"arbitrary_types_allowed": True}
