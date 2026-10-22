"""Link instances: the platform as a *client* of somebody else's server.

A sim instance is a process we own - its runtime answers our control API. A
link instance is the opposite: there is no process of ours, only a TCP
connection to a machine that is already serving Modbus. Everything the UI
shows is a real round trip to that machine, and every write lands on it.

That asymmetry drives the design:

- **One client per instance, kept alive.** Reconnecting per poll would make an
  idle gateway look flaky, so the socket is reused and only re-established when
  it actually breaks.
- **Status costs a round trip.** `state()` cannot be free the way a local
  supervisor's is, so it backs off while a device is unreachable instead of
  spending the request timeout on every poll.
- **Failures are the link's, not the address's.** `ModbusException` means the
  device answered and refused; that is a precise, useful message. Anything else
  is connectivity and is reported as such.
- **Writes are one-way doors.** There is no undo on a real PLC, so the UI has
  to ask before it writes - see the confirm box in the data area editor.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from poly_contracts import RuntimeState
from poly_modbus import ModbusError, ModbusException, ModbusTcpClient

# How often state() is allowed to spend a real round trip. A device that is
# down must not cost the request timeout on every 2.5 s poll.
_HEALTHY_INTERVAL = 1.0
_UNHEALTHY_INTERVAL = 5.0
_EVENTS_KEPT = 60


@dataclass
class LinkTarget:
    host: str
    port: int = 502
    unit_id: int = 1
    timeout: float = 3.0

    @property
    def endpoint(self) -> str:
        return f"modbus-tcp://{self.host}:{self.port}"


@dataclass
class LinkEvent:
    at: float
    ok: bool
    message: str
    latency_ms: int | None = None


class LinkSupervisor:
    """Supervisor surface for a link instance, backed by one Modbus client."""

    def __init__(self, server_id: str, client: ModbusTcpClient) -> None:
        self.server_id = server_id
        self._client = client
        self._events: deque[LinkEvent] = deque(maxlen=_EVENTS_KEPT)
        self._lock = threading.RLock()
        self._status = "stopped"
        self._error: str | None = None
        self._started_at: float | None = None
        self._next_probe = 0.0

    # ------------------------------------------------------------------ life
    def start(self, target: LinkTarget) -> RuntimeState:
        """Connect and prove the device answers before calling it running."""
        self._client.configure(target.host, target.port, target.unit_id, target.timeout)
        with self._lock:
            self._status = "starting"
            self._error = None
            self._started_at = time.time()
            self._next_probe = 0.0
        return self._probe()

    def stop(self) -> None:
        self._client.close()
        with self._lock:
            self._status = "stopped"
            self._error = None
            self._started_at = None
        self._record(True, "已断开连接")

    def forget(self) -> None:
        self.stop()

    # ------------------------------------------------------------------ state
    def state(self) -> RuntimeState:
        with self._lock:
            if self._status == "stopped":
                return self._snapshot()
            if time.monotonic() < self._next_probe:
                return self._snapshot()
            self._next_probe = time.monotonic() + (
                _HEALTHY_INTERVAL if self._status == "running" else _UNHEALTHY_INTERVAL
            )
        return self._probe()

    def values(self) -> RuntimeState:
        return self.state()

    def _probe(self) -> RuntimeState:
        try:
            latency = self._client.probe()
        except ModbusException as exc:
            # The device is there and answered - that is a different story from
            # "cannot reach it", and worth saying precisely.
            with self._lock:
                self._status = "error"
                self._error = str(exc)
            self._record(False, str(exc))
            return self._snapshot()
        except ModbusError as exc:
            with self._lock:
                self._status = "error"
                self._error = str(exc)
            self._record(False, str(exc))
            return self._snapshot()
        with self._lock:
            self._status = "running"
            self._error = None
        self._record(True, "连接正常", latency)
        return self._snapshot()

    def _snapshot(self) -> RuntimeState:
        return RuntimeState(
            server_id=self.server_id,
            status=self._status,  # type: ignore[arg-type]
            started_at=self._started_at,
            error=self._error,
            endpoint=self._client.endpoint,
            latency_ms=self._client.last_latency_ms,
        )

    # ------------------------------------------------------------------ data
    def read_area(self, area: str, address: int, count: int) -> list[Any]:
        try:
            values = self._client.read_area(area, address, count)
        except ModbusError as exc:
            self._note_failure(exc)
            raise
        self._note_success()
        return values

    def write_area(self, area: str, address: int, value: Any) -> None:
        try:
            self._client.write_area(area, address, value)
        except ModbusError as exc:
            self._note_failure(exc)
            raise
        self._note_success(f"写入 {area}[{address}] = {value}")

    def write(self, tag: str, value: Any) -> None:
        raise ModbusError("连接实例没有标签表，请在数据区里按地址写入")

    # ------------------------------------------------------------------ events
    def events(self) -> list[LinkEvent]:
        with self._lock:
            return list(self._events)

    def logs(self, lines: int = 200) -> str:
        return "\n".join(
            f"{time.strftime('%H:%M:%S', time.localtime(event.at))}  "
            f"{'OK  ' if event.ok else 'FAIL'}  {event.message}"
            + (f"  ({event.latency_ms}ms)" if event.latency_ms is not None else "")
            for event in list(self.events())[-lines:]
        )

    # ------------------------------------------------------------------ internals
    def _note_success(self, message: str = "读写成功") -> None:
        with self._lock:
            if self._status != "running":
                self._status = "running"
                self._error = None
                self._started_at = self._started_at or time.time()
        self._record(True, message, self._client.last_latency_ms)

    def _note_failure(self, exc: Exception) -> None:
        with self._lock:
            self._status = "error"
            self._error = str(exc)
            self._next_probe = time.monotonic() + _UNHEALTHY_INTERVAL
        self._record(False, str(exc))

    def _record(self, ok: bool, message: str, latency: int | None = None) -> None:
        with self._lock:
            self._events.append(LinkEvent(at=time.time(), ok=ok, message=message, latency_ms=latency))
