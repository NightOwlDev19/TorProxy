"""
torproxy/router.py
------------------
Parallel Routing Engine

Two routing modes controlled by cfg.parallel_race:

1. RACE MODE (parallel_race=True, default)
   -----------------------------------------
   All available circuits race to fulfil the request simultaneously
   using asyncio.gather().  The first successful response wins; all
   other in-flight tasks are cancelled immediately.
   This minimises latency at the cost of extra outbound connections.

2. SEQUENTIAL FALLBACK (parallel_race=False)
   -------------------------------------------
   Circuits are tried one at a time in random order.  On failure the
   next non-blacklisted circuit is attempted.

Both modes apply the zero-echo policy via RetryManager.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp_socks import ProxyConnector

from .circuit import Circuit
from .config import cfg
from .retry import RetryManager

logger = logging.getLogger("torproxy.router")


# ─────────────────────────────────────────────────────────────────────────────
#  RouteResult
# ─────────────────────────────────────────────────────────────────────────────

class RouteResult:
    """Holds the outcome of a routed request."""

    def __init__(
        self,
        *,
        status: int,
        headers: Dict[str, str],
        body: bytes,
        circuit: Circuit,
        latency_s: float,
        attempts: int,
    ) -> None:
        self.status    = status
        self.headers   = headers
        self.body      = body
        self.circuit   = circuit
        self.latency_s = latency_s
        self.attempts  = attempts

    def __repr__(self) -> str:
        return (
            f"<RouteResult status={self.status} "
            f"via={self.circuit.name} "
            f"latency={self.latency_s*1000:.1f}ms "
            f"attempts={self.attempts}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Router
# ─────────────────────────────────────────────────────────────────────────────

class Router:
    """
    Routes HTTP/HTTPS requests through Tor circuits.

    Parameters
    ----------
    retry_manager : RetryManager
        Shared instance tracking zero-echo stats.
    """

    def __init__(self, retry_manager: RetryManager) -> None:
        self._retry = retry_manager

    # ── Public entry point ────────────────────────────────────────────────────

    async def route(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        request_id: Optional[str] = None,
    ) -> RouteResult:
        """
        Forward *method* + *url* through Tor.
        Applies either race mode or sequential fallback.

        Raises
        ------
        RuntimeError if all circuits fail after cfg.retry_limit attempts.
        """
        request_id = request_id or str(uuid.uuid4())[:8]
        if cfg.parallel_race:
            return await self._race(method, url, headers=headers, body=body, request_id=request_id)
        else:
            return await self._sequential(method, url, headers=headers, body=body, request_id=request_id)

    # ── Race mode ─────────────────────────────────────────────────────────────

    async def _race(
        self,
        method: str,
        url: str,
        *,
        headers,
        body,
        request_id: str,
    ) -> RouteResult:
        """
        Launch a request on ALL available circuits simultaneously.
        Return the first successful response and cancel the rest.
        """
        async with self._retry.session(request_id) as sess:
            for attempt_round in range(cfg.retry_limit):
                pool = sess.available()
                if not pool:
                    break

                logger.info(
                    "[req:%s] race round=%d circuits=%s",
                    request_id, attempt_round, [c.name for c in pool],
                )

                winner: Optional[RouteResult] = None
                tasks: Dict[asyncio.Task, Circuit] = {}

                for circuit in pool:
                    task = asyncio.create_task(
                        self._fetch(circuit, method, url, headers=headers, body=body)
                    )
                    tasks[task] = circuit

                # Wait for first success
                pending = set(tasks.keys())
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for done_task in done:
                        circuit = tasks[done_task]
                        exc = done_task.exception()
                        if exc is None:
                            result = done_task.result()
                            if result is not None:
                                winner = result
                                break
                            else:
                                sess.mark_failed(circuit, "empty response")
                        else:
                            sess.mark_failed(circuit, str(exc))
                    if winner:
                        break

                # Cancel remaining in-flight tasks
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                if winner:
                    return winner

            raise RuntimeError(
                f"[req:{request_id}] All circuits exhausted after {cfg.retry_limit} rounds."
            )

    # ── Sequential fallback mode ──────────────────────────────────────────────

    async def _sequential(
        self,
        method: str,
        url: str,
        *,
        headers,
        body,
        request_id: str,
    ) -> RouteResult:
        """Try circuits one at a time, honouring zero-echo blacklist."""
        async with self._retry.session(request_id) as sess:
            for _ in range(cfg.retry_limit):
                circuit = sess.pick()
                if circuit is None:
                    break
                try:
                    result = await self._fetch(circuit, method, url, headers=headers, body=body)
                    if result is not None:
                        return result
                    sess.mark_failed(circuit, "empty response")
                except Exception as exc:
                    sess.mark_failed(circuit, str(exc))

            raise RuntimeError(
                f"[req:{request_id}] All sequential retries exhausted."
            )

    # ── Core fetch ────────────────────────────────────────────────────────────

    async def _fetch(
        self,
        circuit: Circuit,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]],
        body: Optional[bytes],
    ) -> Optional[RouteResult]:
        """
        Perform one HTTP request through *circuit*'s SOCKS5 port.
        Returns RouteResult on success, raises on error.
        """
        t0 = time.monotonic()
        connector = ProxyConnector.from_url(circuit.socks_url)
        timeout   = aiohttp.ClientTimeout(total=cfg.timeout_sec)

        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout, auto_decompress=False
            ) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    data=body,
                    allow_redirects=False,
                    ssl=False,  # TLS termination is client-side for CONNECT tunnels
                ) as resp:
                    response_body    = await resp.read()
                    response_headers = dict(resp.headers)
                    latency          = time.monotonic() - t0

            circuit.record_success(latency)
            logger.info(
                "[%s] %s %s → %d  (%.0f ms)",
                circuit.name, method, url, resp.status, latency * 1000,
            )
            return RouteResult(
                status=resp.status,
                headers=response_headers,
                body=response_body,
                circuit=circuit,
                latency_s=latency,
                attempts=1,
            )

        except asyncio.CancelledError:
            # Cancelled by race winner — not a real failure
            raise
        except Exception as exc:
            latency = time.monotonic() - t0
            logger.warning(
                "[%s] %s %s FAILED (%.0f ms): %s",
                circuit.name, method, url, latency * 1000, exc,
            )
            raise
