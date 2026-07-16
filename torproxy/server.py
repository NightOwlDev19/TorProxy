"""
torproxy/server.py
------------------
Async HTTP / HTTPS proxy server built using raw asyncio TCP start_server.
This avoids standard web framework routing errors (like 404s on CONNECT).

Supports:
  - Plain HTTP proxying (GET, POST, PUT, DELETE, etc.)
  - HTTPS tunnelling via HTTP CONNECT method
  - Internal stats endpoint showing circuit health and echo rate at /__torproxy__/stats
"""

from __future__ import annotations

import asyncio
import http
import logging
import time
import uuid
from typing import Optional

from .config import cfg
from .router import Router
from .retry import RetryManager
from .tor_controller import TorController

logger = logging.getLogger("torproxy.server")

# Headers that must not be forwarded hop-by-hop
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
    "content-length", "host",
})


def _strip_hop_by_hop(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


class ProxyServer:
    """
    Custom TCP-based HTTP/HTTPS Proxy Server.
    Uses asyncio.start_server to handle connection upgrading (CONNECT) natively.
    """

    def __init__(self) -> None:
        self._tor = TorController()
        self._retry = RetryManager([])
        self._router: Optional[Router] = None
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        logger.info("=== TorProxy starting ===")

        # 1. Start Tor instances
        await self._tor.start()

        # 2. Wire circuits into RetryManager and Router
        self._retry = RetryManager(self._tor.circuits)
        self._router = Router(self._retry)

        # 3. Start TCP server
        self._server = await asyncio.start_server(
            self._handle_client, cfg.proxy_host, cfg.proxy_port
        )

        logger.info(
            "=== TorProxy listening on %s:%d (circuits=%d, race=%s) ===",
            cfg.proxy_host, cfg.proxy_port,
            len(self._tor.available_circuits),
            cfg.parallel_race,
        )

    async def stop(self) -> None:
        logger.info("=== TorProxy shutting down ===")
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self._tor.stop()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # 1. Read first line of request (up to 8KB)
            request_line_bytes = await reader.readline()
            if not request_line_bytes:
                writer.close()
                return

            request_line = request_line_bytes.decode("utf-8", errors="ignore").strip()
            parts = request_line.split()
            if len(parts) < 2:
                writer.close()
                return

            method, target = parts[0].upper(), parts[1]
            request_id = str(uuid.uuid4())[:8]

            # CORS Preflight
            if method == "OPTIONS":
                await self._serve_cors_preflight(writer)
                return

            # stats route
            if cfg.stats_endpoint and "/__torproxy__/stats" in target:
                await self._serve_stats(writer)
                return

            # rotate circuit route
            if "/__torproxy__/rotate" in target:
                await self._handle_rotate_request(writer, target)
                return

            # 3. Dispatch to CONNECT or HTTP
            if method == "CONNECT":
                await self._handle_connect(reader, writer, target, request_id)
            else:
                is_gateway = False
                if target.startswith("/http://") or target.startswith("/https://"):
                    target = target[1:]
                    is_gateway = True
                await self._handle_http(reader, writer, method, target, request_id, is_gateway=is_gateway)

        except Exception as exc:
            logger.error("Error handling request: %s", exc, exc_info=True)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        url: str,
        request_id: str,
        is_gateway: bool = False,
    ) -> None:
        logger.info("[req:%s] HTTP %s %s", request_id, method, url)

        # Read the rest of the HTTP headers
        headers = {}
        while True:
            line_bytes = await reader.readline()
            if not line_bytes or line_bytes == b"\r\n":
                break
            line_str = line_bytes.decode("utf-8", errors="ignore").strip()
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                headers[k.strip()] = v.strip()

        # Read post body if content-length exists
        body = None
        content_length_str = headers.get("Content-Length") or headers.get("content-length")
        if content_length_str:
            try:
                content_length = int(content_length_str)
                body = await reader.readexactly(content_length)
            except Exception as e:
                logger.warning("[req:%s] Error reading body: %s", request_id, e)

        # Route through parallel Tor circuits
        try:
            stripped_headers = _strip_hop_by_hop(headers)
            result = await self._router.route(
                method, url,
                headers=stripped_headers,
                body=body,
                request_id=request_id,
            )
        except Exception as exc:
            logger.error("[req:%s] Routing failed: %s", request_id, exc)
            response = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nContent-Length: 15\r\nConnection: close\r\n\r\nBad Tor Gateway"
            writer.write(response)
            await writer.drain()
            writer.close()
            return

        # Format and write HTTP response back to client
        reason = "OK"
        try:
            reason = http.HTTPStatus(result.status).phrase
        except ValueError:
            pass

        status_line = f"HTTP/1.1 {result.status} {reason}\r\n"
        response_headers = _strip_hop_by_hop(result.headers)
        
        # Make sure content-length matches actual body size
        response_headers["Content-Length"] = str(len(result.body))
        response_headers["Connection"] = "close"

        if is_gateway:
            response_headers["Access-Control-Allow-Origin"] = "*"
            response_headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response_headers["Access-Control-Allow-Headers"] = "*"

        header_lines = [status_line]
        for k, v in response_headers.items():
            header_lines.append(f"{k}: {v}\r\n")
        header_lines.append("\r\n")

        writer.write("".join(header_lines).encode("utf-8"))
        writer.write(result.body)
        await writer.drain()
        writer.close()

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        request_id: str,
    ) -> None:
        logger.info("[req:%s] CONNECT %s", request_id, target)
        try:
            if ":" in target:
                host, port_str = target.split(":", 1)
                port = int(port_str)
            else:
                host = target
                port = 443
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 18\r\n\r\nBad CONNECT target")
            await writer.drain()
            writer.close()
            return

        # Read and discard remaining CONNECT request headers
        while True:
            line_bytes = await reader.readline()
            if not line_bytes or line_bytes == b"\r\n":
                break

        circuits = self._tor.available_circuits
        if not circuits:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 22\r\n\r\nNo Tor circuits active")
            await writer.drain()
            writer.close()
            return

        # Try SOCKS5 connection to target host/port through Tor
        async with self._retry.session(request_id) as sess:
            tunnel_reader = tunnel_writer = None
            circuit = None

            for _ in range(cfg.retry_limit):
                circuit = sess.pick()
                if circuit is None:
                    break
                try:
                    tunnel_reader, tunnel_writer = await self._open_socks5_tunnel(
                        circuit, host, port
                    )
                    break
                except Exception as exc:
                    sess.mark_failed(circuit, str(exc))

            if tunnel_writer is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 24\r\n\r\nCONNECT tunnel failed")
                await writer.drain()
                writer.close()
                return

        # Confirm connection to client
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # Pipe raw bytes bidirectionally
        t0 = time.monotonic()
        try:
            await asyncio.gather(
                self._relay(reader, tunnel_writer),
                self._relay(tunnel_reader, writer),
            )
        except Exception:
            pass
        finally:
            latency = time.monotonic() - t0
            if circuit:
                circuit.record_success(latency)
            try:
                tunnel_writer.close()
                await tunnel_writer.wait_closed()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(
                "[req:%s] CONNECT %s closed (%.0f ms via %s)",
                request_id, target, latency * 1000,
                circuit.name if circuit else "?",
            )

    async def _open_socks5_tunnel(
        self, circuit, host: str, port: int
    ) -> tuple:
        try:
            from python_socks.async_.asyncio import Proxy
        except ImportError:
            raise ImportError(
                "python-socks is required for CONNECT tunneling. "
                "Install with: pip install python-socks"
            )

        proxy = Proxy.from_url(circuit.socks_url)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=cfg.timeout_sec)
        reader, writer = await asyncio.open_connection(sock=sock)
        return reader, writer

    @staticmethod
    async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve_stats(self, writer: asyncio.StreamWriter) -> None:
        import json
        payload = {
            "proxy": {
                "host":         cfg.proxy_host,
                "port":         cfg.proxy_port,
                "parallel_race": cfg.parallel_race,
                "num_circuits": cfg.num_circuits,
            },
            "retry": self._retry.stats(),
            "circuits": self._tor.get_circuit_stats(),
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        headers = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        response = headers + body
        writer.write(response)
        await writer.drain()
        writer.close()

    async def _serve_cors_preflight(self, writer: asyncio.StreamWriter) -> None:
        response = (
            b"HTTP/1.1 204 No Content\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            b"Access-Control-Allow-Headers: *\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(response)
        await writer.drain()
        writer.close()

    async def _handle_rotate_request(self, writer: asyncio.StreamWriter, target: str) -> None:
        import urllib.parse
        parsed = urllib.parse.urlparse(target)
        params = urllib.parse.parse_qs(parsed.query)
        index_str = params.get("index", ["all"])[0]

        rotated = []
        if index_str == "all":
            await self._tor.new_identity_all()
            rotated = [c.name for c in self._tor.circuits]
        else:
            try:
                idx = int(index_str)
                if 0 <= idx < len(self._tor.circuits):
                    circuit = self._tor.circuits[idx]
                    await self._tor.new_identity(circuit)
                    rotated = [circuit.name]
            except ValueError:
                pass

        import json
        payload = {"status": "success", "rotated": rotated}
        body = json.dumps(payload).encode("utf-8")
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        writer.write(response)
        await writer.drain()
        writer.close()
