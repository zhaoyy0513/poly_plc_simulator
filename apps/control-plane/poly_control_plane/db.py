from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


class SiteRow(SQLModel, table=True):
    __tablename__ = "site"

    id: str = Field(primary_key=True)
    name: str
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServerRow(SQLModel, table=True):
    __tablename__ = "server"

    id: str = Field(primary_key=True)
    site_id: str = Field(index=True)
    name: str
    protocol: str
    bind_ip: str = "0.0.0.0"
    port: int
    runtime: str = "native"
    enabled: bool = True
    options: dict = Field(default_factory=dict, sa_column=Column(JSON))
    config_version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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


class AuditRow(SQLModel, table=True):
    __tablename__ = "audit"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = "local"
    action: str
    target: str = ""
    detail: str = Field(default="", sa_column=Column(Text))
