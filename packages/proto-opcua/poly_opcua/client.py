"""OPC UA client - the mirror image of the server adapter.

asyncua's `Client` is async-only; the link layer is synchronous (`probe`,
`read_tags` and `write_tag` are called from FastAPI's threadpool). Bridging the
two needs a **long-lived event loop on its own thread**, not one `asyncio.run`
per call: a fresh loop per request would reconnect every time, and the whole
point of a link is a single kept-alive session.

Three consequences shape this module:

- **The loop outlives the session.** `close()` drops the session but leaves the
  thread running, because a disabled-then-re-enabled link has to reconnect.
  Only `dispose()` - called when the instance is deleted - shuts the thread
  down. Tearing it down in `close()` would make "停用 → 启用" fail forever.
- **No auto-reconnect.** asyncua defaults to `auto_reconnect=False` and we keep
  it: a disabled link must not quietly dial the device again just because
  something polled it. Reconnection happens on the next explicit `probe()`.
- **"Refused" is not "unreachable".** A `BadNodeIdUnknown` is an answer from a
  live server; a timeout is not an answer at all. Only the second turns the
  card red - see `_as_link_error`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from poly_contracts import TagDef
from poly_core import coerce

from asyncua import Client, ua

from .variants import VARIANT, node_id_for


class OpcUaLinkError(RuntimeError):
    """Base error for a link operation."""


class OpcUaLinkConnectionError(OpcUaLinkError):
    """The link could not reach the server, or lost the connection."""


class OpcUaLinkDeviceError(OpcUaLinkError):
    """The server answered and refused the request. The link is still alive."""


def _as_link_error(exc: BaseException, endpoint: str) -> OpcUaLinkError:
    """Sort one asyncua failure into the two categories the supervisor knows.

    `UaError` covers both `UaStatusCodeError` (the server said "no": unknown
    node, wrong type, not writable) and `UaStringParsingError` (the NodeId
    string we were handed cannot be parsed). Both mean the *address* is wrong
    while the connection is fine, so both keep the card green and land in the
    event history instead of being reported as an outage.
    """
    if isinstance(exc, ua.UaError):
        return OpcUaLinkDeviceError(str(exc))
    # Everything else - TimeoutError, ConnectionResetError, a closed transport -
    # is the link itself failing.
    return OpcUaLinkConnectionError(f"{endpoint}: {exc}")


class OpcUaClient:
    """One OPC UA session, driven through a synchronous interface."""

    protocol = "opcua"

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Client | None = None
        self._host = ""
        self._port = 4840
        self._namespace_index = 2
        self._timeout = 3.0
        self._namespaces: list[str] = []
        self._lock = threading.RLock()
        self.last_latency_ms: int | None = None

    # ------------------------------------------------------------------ life
    def configure(
        self,
        host: str,
        port: int,
        namespace_index: int = 2,
        timeout: float = 3.0,
    ) -> None:
        """Remember where to connect. Does not open a session yet."""
        with self._lock:
            if (host, port) != (self._host, self._port):
                self.close()
                self._namespaces = []
            self._host = host
            self._port = port
            self._namespace_index = max(0, int(namespace_index))
            self._timeout = max(0.2, float(timeout))

    def probe(self) -> int:
        """Prove the server answers. Returns the session round trip in ms."""
        with self._lock:
            started = time.monotonic()
            try:
                self._ensure_connected()
                if not self._namespaces:
                    # Read once per session: the namespace index is assigned by
                    # the far server and cannot be guessed, so the dialog shows
                    # this list right after a successful test. A failure here is
                    # not a link failure - it is optional context.
                    try:
                        self._namespaces = list(
                            self._call(self._client.get_namespace_array())
                        )
                    except Exception:
                        pass
            except OpcUaLinkError:
                raise
            except Exception as exc:  # defensive: a broken adapter must not 500
                self._drop()
                raise _as_link_error(exc, self.endpoint) from exc
            self.last_latency_ms = int((time.monotonic() - started) * 1000)
            return self.last_latency_ms

    def close(self) -> None:
        with self._lock:
            self._drop()

    def dispose(self) -> None:
        """Everything goes: session, loop, thread. The instance is gone."""
        with self._lock:
            self._drop()
            loop, self._loop = self._loop, None
            self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # already stopped

    @property
    def connected(self) -> bool:
        """Whether the session is up, without a round trip.

        asyncua exposes this as a `state` property whose enum lives in an
        internal module (`asyncua.client.ua_client`). Comparing its string value
        keeps the check working without importing that path.
        """
        client = self._client
        if client is None:
            return False
        try:
            return getattr(client.state, "value", "") == "connected"
        except Exception:
            return False

    @property
    def endpoint(self) -> str:
        return f"opc.tcp://{self._host}:{self._port}"

    @property
    def namespaces(self) -> list[str]:
        """The far server's namespace table, as of the last probe (may be empty)."""
        with self._lock:
            return list(self._namespaces)

    # ------------------------------------------------------------------ data
    def read_tags(self, tags: list[TagDef]) -> dict[str, Any]:
        """Read a tag table, one node at a time.

        Deliberately not the batched `read_values`: a single unknown NodeId
        would fail the whole batch, and the point of this snapshot is to keep
        the tags that *do* answer visible. A refused node becomes `None` and
        leaves the connection alone; a transport error aborts the snapshot.
        """
        result: dict[str, Any] = {}
        with self._lock:
            self._ensure_connected()
            started = time.monotonic()
            for tag in tags:
                try:
                    result[tag.name] = self._call(self._read_one(tag))
                except OpcUaLinkDeviceError:
                    result[tag.name] = None
                except OpcUaLinkConnectionError:
                    self._drop()
                    raise
            self.last_latency_ms = int((time.monotonic() - started) * 1000)
        return result

    def read_tag(self, tag: TagDef) -> Any:
        with self._lock:
            self._ensure_connected()
            try:
                return self._call(self._read_one(tag))
            except OpcUaLinkDeviceError:
                raise
            except OpcUaLinkConnectionError:
                self._drop()
                raise

    def write_tag(self, tag: TagDef, value: Any) -> None:
        """Write one variable, typed by the tag definition.

        The variant type comes from the tag, not from the Python value: a
        `float` would otherwise be written as a Double and a Float node on the
        far side answers `BadTypeMismatch`. Declaring the type makes a wrong
        tag definition fail loudly instead of silently truncating.
        """
        with self._lock:
            self._ensure_connected()
            started = time.monotonic()
            try:
                node = self._client.get_node(node_id_for(tag, self._namespace_index))
                self._call(node.write_value(coerce(value, tag.data_type), VARIANT[tag.data_type]))
            except OpcUaLinkDeviceError:
                raise
            except OpcUaLinkConnectionError:
                self._drop()
                raise
            except Exception as exc:  # pragma: no cover - asyncua internals
                if not isinstance(exc, ua.UaError):
                    self._drop()
                raise _as_link_error(exc, self.endpoint) from exc
            self.last_latency_ms = int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------------ internals
    def _read_one(self, tag: TagDef):
        """The coroutine for one read, with asyncua failures already sorted."""
        node_id = node_id_for(tag, self._namespace_index)

        async def run() -> Any:
            try:
                return await self._client.get_node(node_id).read_value()
            except Exception as exc:
                raise _as_link_error(exc, self.endpoint) from exc

        return run()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None:
            return loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=self._run_loop, args=(loop,), name="opcua-link", daemon=True
        )
        thread.start()
        self._loop = loop
        self._thread = thread
        return loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _call(self, coro, timeout: float | None = None):
        """Run one coroutine on the link's own loop and wait for it.

        The budget is the configured request timeout plus a little slack for the
        session handshake (Hello, OpenSecureChannel, CreateSession,
        ActivateSession - four round trips before the first request). Without a
        ceiling a half-open TCP connection would block a request thread for as
        long as the OS keeps retrying.
        """
        loop = self._ensure_loop()
        budget = timeout if timeout is not None else self._timeout + 2.0
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(budget)
        except TimeoutError as exc:
            # Leave no coroutine running behind the failed request.
            future.cancel()
            raise OpcUaLinkConnectionError(f"{self.endpoint}: 请求超时（{budget:.1f}s）") from exc

    def _ensure_connected(self) -> None:
        if self.connected:
            return
        self._drop()
        if not self._host:
            raise OpcUaLinkConnectionError("还没有填目标地址")
        client = Client(url=self.endpoint, timeout=self._timeout)
        try:
            self._call(client.connect())
        except Exception as exc:
            try:
                self._call(client.disconnect(), timeout=2.0)
            except Exception:
                pass
            if isinstance(exc, OpcUaLinkError):
                raise
            raise _as_link_error(exc, self.endpoint) from exc
        self._client = client

    def _drop(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            self._call(client.disconnect(), timeout=2.0)
        except Exception:
            pass
