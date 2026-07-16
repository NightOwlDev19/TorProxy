"""
tests/test_retry.py
-------------------
Unit tests for the zero-echo RetryManager.
"""

import pytest
from unittest.mock import MagicMock

from torproxy.circuit import Circuit, CircuitState
from torproxy.retry import RetryManager, RetrySession


def _make_circuit(index: int) -> Circuit:
    c = Circuit(index, socks_port=9050 + index * 2, control_port=9051 + index * 2)
    c.state = CircuitState.READY
    return c


@pytest.fixture
def circuits():
    return [_make_circuit(i) for i in range(3)]


@pytest.fixture
def manager(circuits):
    return RetryManager(circuits)


# ── Zero-echo guarantee ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_circuit_never_repicked(manager, circuits):
    """A circuit that fails must never be picked again for the same request."""
    async with manager.session("req-test") as sess:
        picked = set()
        for _ in range(3):
            c = sess.pick()
            if c is None:
                break
            assert c.index not in picked, "Same circuit picked twice (echo!)"
            picked.add(c.index)
            sess.mark_failed(c, "timeout")
        # All circuits exhausted, next pick must be None
        assert sess.pick() is None


@pytest.mark.asyncio
async def test_echo_events_zero(manager, circuits):
    """Echo events must be 0 when using the API correctly."""
    async with manager.session("req-echo") as sess:
        for _ in range(len(circuits)):
            c = sess.pick()
            if c:
                sess.mark_failed(c, "err")
        assert sess.echo_events == 0


@pytest.mark.asyncio
async def test_successful_pick_not_blacklisted(manager, circuits):
    """Successful circuits stay available."""
    async with manager.session("req-success") as sess:
        c1 = sess.pick()
        assert c1 is not None
        # We do NOT mark it failed → it should remain available
        pool_after = [x.index for x in sess.available()]
        assert c1.index in pool_after


@pytest.mark.asyncio
async def test_exhausted_flag(manager, circuits):
    """sess.exhausted becomes True once all circuits are blacklisted."""
    async with manager.session("req-exhaust") as sess:
        assert not sess.exhausted
        for c in circuits:
            sess.mark_failed(c, "err")
        assert sess.exhausted


# ── Stats ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_global_echo_rate_zero(manager, circuits):
    """Global echo_rate must be 0.0 under normal operation."""
    for i in range(5):
        async with manager.session(f"req-{i}") as sess:
            while True:
                c = sess.pick()
                if c is None:
                    break
                sess.mark_failed(c, "err")
    assert manager.echo_rate == 0.0
