"""Runtime that hosts exactly one server instance.

One OS process per instance is what gives us the isolation the product needs:
a crashing adapter, an exhausted thread or a blocking library can only take
down its own instance.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from poly_contracts import Protocol, RuntimeState, ServerConfig, SimMode
from poly_core import ProtocolAdapter, next_value

PROTOCOLS: dict[Protocol, str] = {
    Protocol.MODBUS_TCP: "poly_modbus:ModbusTcpAdapter",
    Protocol.S7: "poly_s7:S7Adapter",
    Protocol.OPCUA: "poly_opcua:OpcUaAdapter",
}


def create_adapter(config: ServerConfig) -> ProtocolAdapter:
    target = PROTOCOLS[config.protocol]
    module_name, class_name = target.split(":")
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)(config)


class RuntimeRunner:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.adapter: ProtocolAdapter | None = None
        self.status = "starting"
        self.error: str | None = None
        self.started_at: float | None = None
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._last: dict[str, Any] = {}

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        try:
            adapter = create_adapter(self.config)
            adapter.start()
            self.adapter = adapter
            self.started_at = time.time()
            self.status = "running"
            self._ticker = threading.Thread(target=self._tick, name="sim-ticker", daemon=True)
            self._ticker.start()
        except Exception as exc:  # surfaced to the agent, never swallowed
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            raise

    def stop(self) -> None:
        self._stop.set()
        if self.adapter is not None:
            try:
                self.adapter.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------ sim
    def _tick(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(0.2):
            if self.adapter is None:
                continue
            elapsed = time.monotonic() - start
            for tag in self.config.tags:
                if tag.sim.mode is SimMode.STATIC:
                    continue
                try:
                    value = next_value(tag, elapsed, self._last.get(tag.name))
                    if value is None:
                        continue
                    self.adapter.write_tag(tag, value)
                    self._last[tag.name] = value
                except Exception:
                    continue

    # ------------------------------------------------------------------ api
    def write(self, name: str, value: Any) -> None:
        if self.adapter is None:
            raise RuntimeError(self.error or "runtime is not running")
        tag = self.adapter.tags.get(name)
        if tag is None:
            raise KeyError(f"unknown tag: {name}")
        if not tag.writable:
            raise PermissionError(f"tag is read-only: {name}")
        self.adapter.write_tag(tag, value)

    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        if self.adapter is None:
            raise RuntimeError(self.error or "runtime is not running")
        return self.adapter.read_area(area, address, count)

    def write_area(self, area: str, address: int, value: Any) -> None:
        if self.adapter is None:
            raise RuntimeError(self.error or "runtime is not running")
        self.adapter.write_area(area, address, value)

    def state(self) -> RuntimeState:
        values: dict[str, Any] = {}
        clients = 0
        endpoint = None
        if self.adapter is not None:
            try:
                values = self.adapter.snapshot()
                clients = int(getattr(self.adapter, "clients", 0) or 0)
                endpoint = self.adapter.endpoint()
            except Exception as exc:
                self.error = f"snapshot failed: {exc}"
        return RuntimeState(
            server_id=self.config.id,
            status=self.status,  # type: ignore[arg-type]
            started_at=self.started_at,
            error=self.error,
            values=values,
            clients=clients,
            endpoint=endpoint,
        )
