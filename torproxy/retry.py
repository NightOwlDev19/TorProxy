"""
torproxy/retry.py
-----------------
Zero-Echo RetryManager

The "zero-echo" policy guarantees that if a circuit fails for a specific
request, that circuit is NEVER retried for the same request.  This is
tracked per-request via a blacklist set.

Echo rate = (retries on same failed circuit) / total retries.
Target: 0%.

Usage
-----
    mgr = RetryManager(circuits)
    async with mgr.session(request_id) as session:
        circuit = session.pick()            # never returns a blacklisted one
        try:
            ...
        except Exception as exc:
            session.mark_failed(circuit, str(exc))
            circuit = session.pick()        # guaranteed different circuit
"""

from __future__ import annotations

import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List, Optional, Set

from .circuit import Circuit

logger = logging.getLogger("torproxy.retry")


@dataclass
class RetrySession:
    """
    Tracks the retry state for a single forwarded request.

    Attributes
    ----------
    request_id      : unique per-request identifier (used for logging)
    _circuits       : reference to the full circuit pool
    _blacklist      : circuits that have already failed for THIS request
    _attempt        : total attempt count
    _echo_events    : would-be echoes that were prevented
    """

    request_id: str
    _circuits:  List[Circuit]
    _blacklist: Set[int]  = field(default_factory=set)   # set of circuit.index
    _attempt:   int       = 0
    _echo_events: int     = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def available(self) -> List[Circuit]:
        """Return circuits that are usable AND not blacklisted for this request."""
        return [
            c for c in self._circuits
            if c.is_available and c.index not in self._blacklist
        ]

    def pick(self) -> Optional[Circuit]:
        """
        Select the next circuit to try (never a previously-failed one).
        Returns None if no circuits are left.
        """
        pool = self.available()
        if not pool:
            logger.warning(
                "[req:%s] No circuits left after %d attempts (blacklist=%s)",
                self.request_id, self._attempt, self._blacklist,
            )
            return None
        chosen = random.choice(pool)
        self._attempt += 1
        logger.debug(
            "[req:%s] attempt=%d chose %s (blacklist size=%d)",
            self.request_id, self._attempt, chosen.name, len(self._blacklist),
        )
        return chosen

    def mark_failed(self, circuit: Circuit, reason: str = "") -> None:
        """
        Record *circuit* as failed for this request.
        Adds to blacklist — it will never be picked again for this request.
        """
        if circuit.index in self._blacklist:
            # This would be an echo — should never happen with correct usage.
            self._echo_events += 1
            circuit.stats.echo_events += 1
            logger.error(
                "[req:%s] ECHO DETECTED on %s (this is a bug!) reason=%s",
                self.request_id, circuit.name, reason,
            )
        else:
            self._blacklist.add(circuit.index)
            circuit.record_failure(reason)
            logger.info(
                "[req:%s] %s blacklisted for this request. reason=%s",
                self.request_id, circuit.name, reason,
            )

    @property
    def attempts(self) -> int:
        return self._attempt

    @property
    def echo_events(self) -> int:
        return self._echo_events

    @property
    def exhausted(self) -> bool:
        return len(self.available()) == 0


class RetryManager:
    """
    Factory for RetrySession objects.
    Also tracks global echo statistics across all requests.
    """

    def __init__(self, circuits: List[Circuit]) -> None:
        self._circuits      = circuits
        self.total_requests = 0
        self.total_echoes   = 0   # cumulative; target 0

    @asynccontextmanager
    async def session(self, request_id: str):
        """Async context manager that yields a fresh RetrySession."""
        sess = RetrySession(
            request_id=request_id,
            _circuits=self._circuits,
        )
        self.total_requests += 1
        try:
            yield sess
        finally:
            if sess.echo_events > 0:
                self.total_echoes += sess.echo_events
                logger.warning(
                    "Global echo count: %d (should be 0)", self.total_echoes
                )

    @property
    def echo_rate(self) -> float:
        """Echo rate across all requests. Should be 0.0."""
        if self.total_requests == 0:
            return 0.0
        return self.total_echoes / self.total_requests

    def stats(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_echoes":   self.total_echoes,
            "echo_rate":      self.echo_rate,
        }
