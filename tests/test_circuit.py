"""
tests/test_circuit.py
----------------------
Unit tests for the Circuit abstraction and its stats.
"""

import pytest
from torproxy.circuit import Circuit, CircuitState, CircuitStats


def _make_ready_circuit(index: int = 0) -> Circuit:
    c = Circuit(index, socks_port=9050, control_port=9051)
    c.mark_ready()
    return c


# ── State transitions ─────────────────────────────────────────────────────────

def test_initial_state_is_starting():
    c = Circuit(0, 9050, 9051)
    assert c.state == CircuitState.STARTING


def test_mark_ready():
    c = _make_ready_circuit()
    assert c.state == CircuitState.READY
    assert c.is_available is True


def test_mark_degraded():
    c = _make_ready_circuit()
    c.mark_degraded("test reason")
    assert c.state == CircuitState.DEGRADED
    assert c.is_available is True   # degraded is still usable


def test_mark_dead():
    c = _make_ready_circuit()
    c.mark_dead("unrecoverable")
    assert c.state == CircuitState.DEAD
    assert c.is_available is False


def test_success_recovers_degraded():
    c = _make_ready_circuit()
    c.mark_degraded("network hiccup")
    c.record_success(0.5)
    assert c.state == CircuitState.READY


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_success_rate_initially_one():
    c = _make_ready_circuit()
    assert c.stats.success_rate == 1.0


def test_success_rate_after_failures():
    c = _make_ready_circuit()
    c.record_success(0.1)
    c.record_success(0.2)
    c.record_failure("err")
    # 2 ok / 3 total
    assert abs(c.stats.success_rate - 2 / 3) < 1e-9


def test_avg_latency():
    c = _make_ready_circuit()
    c.record_success(1.0)
    c.record_success(3.0)
    assert c.stats.avg_latency_s == pytest.approx(2.0)


def test_to_dict_keys():
    c = _make_ready_circuit()
    d = c.to_dict()
    for key in ("name", "socks_port", "control_port", "state",
                "requests_total", "requests_ok", "success_rate",
                "avg_latency_ms", "echo_events"):
        assert key in d, f"Missing key: {key}"


# ── SOCKS URL ─────────────────────────────────────────────────────────────────

def test_socks_url():
    c = Circuit(1, 9052, 9053)
    assert c.socks_url == "socks5://127.0.0.1:9052"
