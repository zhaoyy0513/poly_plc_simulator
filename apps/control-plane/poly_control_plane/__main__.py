from __future__ import annotations

import argparse
import os
import uuid

import uvicorn


def _init_admin(name: str) -> None:
    """Create an admin user (default password 123456) if none exist yet.

    Runs before uvicorn starts so the table is ready and the operator can log
    in on first boot. Idempotent: if any user already exists it prints and
    returns. Schema is ensured here so the call works on a fresh data dir.
    """
    from poly_control_plane.auth import DEFAULT_PASSWORD, hash_password
    from poly_control_plane.db import UserRow, engine
    from poly_control_plane.migrate import ensure_columns
    from sqlmodel import Session, SQLModel, select

    SQLModel.metadata.create_all(engine)
    ensure_columns(engine)
    with Session(engine) as session:
        existing = session.exec(select(UserRow)).first()
        if existing is not None:
            print(f"用户已存在（如 {existing.username}），跳过管理员创建")
            return
        username = name.strip()
        if not username:
            raise SystemExit("--init-admin 需要一个非空用户名")
        row = UserRow(
            id=uuid.uuid4().hex[:12],
            username=username,
            password_hash=hash_password(DEFAULT_PASSWORD),
            is_admin=True,
            must_change_password=True,
        )
        session.add(row)
        session.commit()
        print(
            f"已创建管理员「{row.username}」（默认密码 {DEFAULT_PASSWORD}，请尽快修改）"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poly PLC simulator control plane")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=25000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--stop-on-exit",
        action="store_true",
        help="stop this machine's simulator runtimes when the control plane exits "
        "(default: leave them running and adopt them on the next start)",
    )
    parser.add_argument(
        "--init-admin",
        metavar="NAME",
        default=None,
        help="create an admin user (default password 123456) if no users exist, then start",
    )
    args = parser.parse_args(argv)
    if args.stop_on_exit:
        os.environ["POLY_STOP_RUNTIMES_ON_EXIT"] = "1"
    if args.init_admin is not None:
        _init_admin(args.init_admin)
    uvicorn.run(
        "poly_control_plane.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
