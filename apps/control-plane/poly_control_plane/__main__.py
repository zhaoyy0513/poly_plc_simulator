from __future__ import annotations

import argparse
import os

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poly PLC simulator control plane")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=25011)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--stop-on-exit",
        action="store_true",
        help="stop this machine's simulator runtimes when the control plane exits "
        "(default: leave them running and adopt them on the next start)",
    )
    args = parser.parse_args(argv)
    if args.stop_on_exit:
        os.environ["POLY_STOP_RUNTIMES_ON_EXIT"] = "1"
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
