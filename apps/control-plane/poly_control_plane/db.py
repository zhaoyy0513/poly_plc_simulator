from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, Session, SQLModel, create_engine

from .system import data_dir


class SiteRow(SQLModel, table=True):
    __tablename__ = "site"

    id: str = Field(primary_key=True)
    name: str
    description: str = ""
    # A site is one target machine. An empty host means "this machine" and is
    # supervised in-process; anything else is reached over SSH.
    host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_secret: str = ""  # encrypted at rest, never returned by the API
    deploy_dir: str = "~/.poly-sim"
    python_bin: str = ""
    last_probe_at: str = ""
    last_probe: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_local(self) -> bool:
        return not self.host.strip()


class ServerRow(SQLModel, table=True):
    __tablename__ = "server"

    id: str = Field(primary_key=True)
    site_id: str = Field(index=True)
    name: str
    protocol: str
    # 'sim' = we deploy and run it; 'link' = we connect to someone else's.
    kind: str = "sim"
    bind_ip: str = "0.0.0.0"
    port: int
    runtime: str = "native"
    # Link only: the server we talk to. `port` mirrors `target_port` so the
    # existing cards keep showing the address that matters.
    target_host: str = ""
    target_port: int = 0
    target_options: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # Loopback control port on the instance's own machine. Local instances
    # leave it 0 and get an ephemeral port; remote ones need it fixed so the
    # platform can find the runtime again through the SSH channel.
    control_port: int = 0
    enabled: bool = True
    options: dict = Field(default_factory=dict, sa_column=Column(JSON))
    config_version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_link(self) -> bool:
        return self.kind == "link"


class TagRow(SQLModel, table=True):
    __tablename__ = "tag"

    id: str = Field(primary_key=True)
    server_id: str = Field(index=True)
    position: int = 0
    name: str
    data_type: str
    area: str
    address: int
    bit: int | None = None
    db_number: int | None = None
    word_order: str = "big"
    writable: bool = True
    description: str = ""
    sim: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # OPC UA only: the full NodeId on the far side, when it is not derivable
    # from the namespace index plus the tag name.
    node_id: str | None = None


class AuditRow(SQLModel, table=True):
    __tablename__ = "audit"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = "local"
    action: str
    target: str = ""
    detail: str = Field(default="", sa_column=Column(Text))
    # Structured request params for the audited action (protocol, port, kind,
    # write value, ...). `detail` stays the human summary; this is the machine view.
    params: dict = Field(default_factory=dict, sa_column=Column(JSON))


class UserRow(SQLModel, table=True):
    __tablename__ = "user"

    id: str = Field(primary_key=True)
    username: str = Field(unique=True, index=True)
    # Format: pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64> (stdlib only)
    password_hash: str
    is_admin: bool = False
    # True until the user changes the default password (123456).
    must_change_password: bool = True
    # Bumped on every password change; Fernet tokens carry this version.
    token_version: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# The engine and session factory live with the schema, not the app, so any
# module (auth, deploy, ...) can depend on them without importing the FastAPI
# app and creating a cycle.
DB_PATH = data_dir() / "app.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def get_session() -> Any:
    with Session(engine) as session:
        yield session
