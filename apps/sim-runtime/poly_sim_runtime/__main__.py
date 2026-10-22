from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from poly_contracts import ServerConfig

from .control import create_app
from .runner import RuntimeRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one PLC simulator instance")
    parser.add_argument("--config", required=True, help="path to ServerConfig json")
    parser.add_argument("--control-port", type=int, required=True, help="loopback control port")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    config = ServerConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
    runner = RuntimeRunner(config)
    try:
        runner.start()
    except Exception as exc:
        print(f"[runtime] failed to start {config.id}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        # Still serve /state so the agent can report the real error instead of "exited".

    app = create_app(runner)
    print(
        f"[runtime] {config.protocol.value} '{config.name}' on {config.bind_ip}:{config.port} "
        f"control={args.host}:{args.control_port}",
        flush=True,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.control_port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
