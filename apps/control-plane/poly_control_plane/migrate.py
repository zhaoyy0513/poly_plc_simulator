"""Idempotent SQLite schema migrations.

`create_all()` only creates tables that do not exist; it never adds a column
to a table that is already there. Until Alembic is wired in, every schema
change is applied here at startup by diffing `PRAGMA table_info` against the
columns we expect. Safe to run on every boot.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

# table -> {column: DDL used by ALTER TABLE ADD COLUMN}
# The DEFAULT is mandatory: it backfills existing rows instead of leaving NULL.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "site": {
        "host": "TEXT DEFAULT ''",
        "ssh_port": "INTEGER DEFAULT 22",
        "ssh_user": "TEXT DEFAULT ''",
        "ssh_secret": "TEXT DEFAULT ''",
        "deploy_dir": "TEXT DEFAULT '~/.poly-sim'",
        "python_bin": "TEXT DEFAULT ''",
        "last_probe_at": "TEXT DEFAULT ''",
        "last_probe": "TEXT DEFAULT '{}'",
    },
    "server": {
        "kind": "TEXT DEFAULT 'sim'",
        "target_host": "TEXT DEFAULT ''",
        "target_port": "INTEGER DEFAULT 0",
        "target_options": "TEXT DEFAULT '{}'",
        "control_port": "INTEGER DEFAULT 0",
    },
    "tag": {
        # NULL, not '': an empty NodeId means "derive it from the link's
        # namespace index", which is a different thing from an explicit one.
        "node_id": "TEXT DEFAULT NULL",
    },
    "audit": {
        # JSON stored as TEXT; empty object backfills existing rows.
        "params": "TEXT DEFAULT '{}'",
    },
}


def ensure_columns(engine: Engine) -> list[str]:
    """Add every missing column. Returns the ones it actually added."""
    added: list[str] = []
    with engine.connect() as connection:
        for table, columns in EXPECTED_COLUMNS.items():
            existing = {
                row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns.items():
                if name in existing:
                    continue
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
        if added:
            connection.commit()
    return added
