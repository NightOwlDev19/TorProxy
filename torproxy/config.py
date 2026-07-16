"""
torproxy/config.py
------------------
Central configuration for TorProxy.
All values can be overridden via environment variables or a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; fall back to real env vars


@dataclass
class Config:
    # ── Proxy listener ───────────────────────────────────────────────────────
    proxy_host: str = field(default_factory=lambda: os.getenv("PROXY_HOST", "0.0.0.0"))
    proxy_port: int = field(default_factory=lambda: int(os.getenv("PROXY_PORT", "8080")))

    # ── Tor instances ─────────────────────────────────────────────────────────
    num_circuits: int = field(default_factory=lambda: int(os.getenv("NUM_CIRCUITS", "3")))
    socks_base_port: int = field(
        default_factory=lambda: int(os.getenv("SOCKS_BASE_PORT", "9050"))
    )
    control_base_port: int = field(
        default_factory=lambda: int(os.getenv("CONTROL_BASE_PORT", "9051"))
    )
    tor_binary: str = field(
        default_factory=lambda: os.getenv("TOR_BINARY", "tor")
    )
    tor_data_dir: str = field(
        default_factory=lambda: os.getenv(
            "TOR_DATA_DIR",
            str(Path(__file__).resolve().parent.parent / "tor_data"),
        )
    )
    torrc_template: str = field(
        default_factory=lambda: os.getenv(
            "TORRC_TEMPLATE",
            str(Path(__file__).resolve().parent.parent / "torrc.template"),
        )
    )

    # ── Routing / retry ───────────────────────────────────────────────────────
    timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("TIMEOUT_SEC", "30"))
    )
    retry_limit: int = field(
        default_factory=lambda: int(os.getenv("RETRY_LIMIT", "5"))
    )
    # Race mode: True = first-wins parallel, False = sequential fallback
    parallel_race: bool = field(
        default_factory=lambda: os.getenv("PARALLEL_RACE", "true").lower() == "true"
    )

    # ── Control auth ─────────────────────────────────────────────────────────
    control_password: str = field(
        default_factory=lambda: os.getenv("CONTROL_PASSWORD", "torproxy_secret")
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )
    stats_endpoint: bool = field(
        default_factory=lambda: os.getenv("STATS_ENDPOINT", "true").lower() == "true"
    )

    # ── Derived helpers ───────────────────────────────────────────────────────
    def socks_port(self, index: int) -> int:
        """Return the SOCKS5 port for circuit at *index*."""
        return self.socks_base_port + index * 2

    def control_port(self, index: int) -> int:
        """Return the control port for circuit at *index*."""
        return self.control_base_port + index * 2


# Singleton accessed by all modules
cfg = Config()
