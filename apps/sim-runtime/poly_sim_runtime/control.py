"""Loopback-only control API of one runtime.

It is bound to 127.0.0.1 and an ephemeral port chosen by the agent, so it is
never reachable from the network. The agent is the only client.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .runner import RuntimeRunner


class WriteBody(BaseModel):
    tag: str
    value: Any


class AreaWriteBody(BaseModel):
    area: str
    address: int
    value: Any


def create_app(runner: RuntimeRunner) -> FastAPI:
    app = FastAPI(title="poly sim runtime", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": runner.status == "running", "status": runner.status, "error": runner.error}

    @app.get("/state")
    def state() -> dict[str, Any]:
        return runner.state().model_dump(mode="json")

    @app.post("/write")
    def write(body: WriteBody) -> dict[str, Any]:
        try:
            runner.write(body.tag, body.value)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/area")
    def area(area: str = "holding", address: int = 0, count: int = 50) -> dict[str, Any]:
        try:
            values = runner.read_area(area, address, count)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"area": area, "address": address, "count": count, "values": values}

    @app.post("/area/write")
    def write_area(body: AreaWriteBody) -> dict[str, Any]:
        try:
            runner.write_area(body.area, body.address, body.value)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/connections")
    def connections() -> dict[str, Any]:
        adapter = runner.adapter
        return {"connections": adapter.connections() if adapter is not None else []}

    @app.post("/connections/{conn_id}/kick")
    def kick_connection(conn_id: str) -> dict[str, Any]:
        adapter = runner.adapter
        if adapter is None:
            raise HTTPException(status_code=404, detail="实例未在运行")
        try:
            adapter.kick(conn_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "kicked": conn_id}

    return app
