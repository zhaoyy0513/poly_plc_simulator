"""OPC UA server adapter.

The tag table becomes a folder of variables under Objects/. Variable values are
the source of truth, so writes made by an OPC UA client show up in the console
without any extra plumbing.

The advertised endpoint URL is configurable on purpose: auto-detection yields
127.0.0.1 or a container hostname, and clients then fail to connect even though
the TCP port is reachable.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

from poly_contracts import ServerConfig, TagDef
from poly_core import ProtocolAdapter, coerce, seed_value

from asyncua import Server, ua

from .variants import VARIANT


def detect_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        try:
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"


class OpcUaAdapter(ProtocolAdapter):
    protocol = "opcua"

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Server | None = None
        self._nodes: dict[str, ua.Node] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    def endpoint_url(self) -> str:
        options = self.config.options
        if options.endpoint_url:
            return options.endpoint_url
        host = options.advertise_host
        if not host:
            host = self.config.bind_ip if self.config.bind_ip not in ("0.0.0.0", "::") else detect_lan_ip()
        return f"opc.tcp://{host}:{self.config.port}"

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="opcua-loop", daemon=True)
        self._thread.start()
        self._call(self._start(), timeout=20)

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float = 5.0):
        if self._loop is None:
            raise RuntimeError("OPC UA loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    async def _start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint_url())
        try:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        except Exception:
            pass
        namespace = self.config.options.namespace or "urn:poly:plc"
        index = await server.register_namespace(namespace)
        folder = await server.nodes.objects.add_folder(index, self._folder_name())
        for tag in self.tags.values():
            value = coerce(seed_value(tag), tag.data_type)
            node = await folder.add_variable(
                ua.NodeId(tag.name, index),
                tag.name,
                value,
                varianttype=VARIANT[tag.data_type],
            )
            if tag.writable:
                await node.set_writable()
            if tag.description:
                await node.write_description(ua.LocalizedText(tag.description))
            self._nodes[tag.name] = node
        await server.start()
        self._server = server

    def _folder_name(self) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in self.config.name)
        return safe or "PLC"

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._call(self._server.stop(), timeout=5)
        except Exception:
            pass
        finally:
            self._server = None
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------ data
    def read_tag(self, tag: TagDef) -> Any:
        node = self._nodes.get(tag.name)
        if node is None:
            return None
        return self._call(node.read_value(), timeout=2)

    def write_tag(self, tag: TagDef, value: Any) -> None:
        node = self._nodes.get(tag.name)
        if node is None:
            return
        variant = ua.Variant(coerce(value, tag.data_type), VARIANT[tag.data_type])
        self._call(node.write_value(ua.DataValue(variant)), timeout=2)

    def reset(self) -> None:
        for tag in self.tags.values():
            if tag.name in self._nodes:
                self.write_tag(tag, coerce(seed_value(tag), tag.data_type))

    def snapshot(self) -> dict[str, Any]:
        if not self._nodes:
            return {}
        return self._call(self._snapshot_async(), timeout=5)

    async def _snapshot_async(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, node in self._nodes.items():
            try:
                result[name] = await node.read_value()
            except Exception:
                result[name] = None
        return result

    def endpoint(self) -> str:
        return self.endpoint_url()
