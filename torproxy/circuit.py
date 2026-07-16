"""
torproxy/circuit.py
-------------------
Abstraction for a single Tor circuit (one Tor process instance).
Each circuit exposes a SOCKS5 port and tracks its own health / stats.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger("torproxy.circuit")


class CircuitState(Enum):
    STARTING = auto()
    READY    = auto()
    DEGRADED = auto()
    DEAD     = auto()


@dataclass
class CircuitStats:
    """Running statistics for a single circuit."""
    requests_total:  int   = 0
    requests_ok:     int   = 0
    requests_failed: int   = 0
    total_latency_s: float = 0.0
    last_used:       float = field(default_factory=time.monotonic)
    last_error:      Optional[str] = None

    # ── zero-echo tracking ──────────────────────────────────────────────────
    # How many times this circuit was skipped because it had already failed
    # for a given request (echo events). Goal = 0.
    echo_events: int = 0

    @property
    def success_rate(self) -> float:
        if self.requests_total == 0:
            return 1.0
        return self.requests_ok / self.requests_total

    @property
    def avg_latency_s(self) -> float:
        if self.requests_ok == 0:
            return 0.0
        return self.total_latency_s / self.requests_ok

    def record_success(self, latency_s: float) -> None:
        self.requests_total += 1
        self.requests_ok    += 1
        self.total_latency_s += latency_s
        self.last_used = time.monotonic()

    def record_failure(self, error: str) -> None:
        self.requests_total  += 1
        self.requests_failed += 1
        self.last_error = error
        self.last_used = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "requests_total":  self.requests_total,
            "requests_ok":     self.requests_ok,
            "requests_failed": self.requests_failed,
            "success_rate":    round(self.success_rate, 4),
            "avg_latency_ms":  round(self.avg_latency_s * 1000, 2),
            "echo_events":     self.echo_events,
            "last_error":      self.last_error,
        }


class Circuit:
    """
    Represents one Tor process instance (one SOCKS5 port).

    Attributes
    ----------
    index       : sequential number (0, 1, 2…)
    socks_port  : SOCKS5 port this instance listens on
    control_port: Tor control port for NEWNYM / health-checks
    state       : current CircuitState
    stats       : CircuitStats instance
    """

    def __init__(self, index: int, socks_port: int, control_port: int) -> None:
        self.index        = index
        self.socks_port   = socks_port
        self.control_port = control_port
        self.state        = CircuitState.STARTING
        self.stats        = CircuitStats()
        self._lock        = asyncio.Lock()

    # ── Identity helpers ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return f"circuit-{self.index}"

    @property
    def socks_url(self) -> str:
        return f"socks5://127.0.0.1:{self.socks_port}"

    @property
    def is_available(self) -> bool:
        return self.state in (CircuitState.READY, CircuitState.DEGRADED)

    # ── State transitions ─────────────────────────────────────────────────────

    def mark_ready(self) -> None:
        self.state = CircuitState.READY
        logger.info("[%s] → READY (socks=%d ctrl=%d)", self.name, self.socks_port, self.control_port)

    def mark_degraded(self, reason: str = "") -> None:
        if self.state != CircuitState.DEAD:
            self.state = CircuitState.DEGRADED
            logger.warning("[%s] → DEGRADED: %s", self.name, reason)

    def mark_dead(self, reason: str = "") -> None:
        self.state = CircuitState.DEAD
        logger.error("[%s] → DEAD: %s", self.name, reason)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def record_success(self, latency_s: float) -> None:
        self.stats.record_success(latency_s)
        if self.state == CircuitState.DEGRADED:
            self.state = CircuitState.READY  # recover on success

    def record_failure(self, error: str) -> None:
        self.stats.record_failure(error)
        if self.stats.requests_failed >= 3 and self.stats.success_rate < 0.3:
            self.mark_degraded(error)

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "socks_port":   self.socks_port,
            "control_port": self.control_port,
            "state":        self.state.name,
            **self.stats.to_dict(),
        }

    def __repr__(self) -> str:
        return f"<Circuit {self.name} socks={self.socks_port} state={self.state.name}>"
