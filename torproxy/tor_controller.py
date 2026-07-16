"""
torproxy/tor_controller.py
--------------------------
Manages multiple Tor process instances.
Each instance is bound to its own SOCKS5 + control port pair.

Uses the `stem` library to:
  - Authenticate against the Tor control port
  - Send NEWNYM (new identity / circuit rotation) signals
  - Query circuit health

If `stem` is not installed the controller falls back to "no-control" mode
(processes are launched but cannot be signalled for NEWNYM).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from string import Template
from typing import Dict, List, Optional

from .circuit import Circuit, CircuitState
from .config import cfg

logger = logging.getLogger("torproxy.tor_controller")

# ── Optional stem import ──────────────────────────────────────────────────────
try:
    import stem
    import stem.control
    import stem.process
    import stem.connection
    _STEM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _STEM_AVAILABLE = False
    logger.warning("stem library not found — circuit rotation (NEWNYM) disabled.")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hashed_password(password: str) -> str:
    """Return a Tor-compatible HashedControlPassword for *password*."""
    if not _STEM_AVAILABLE:
        return ""
    return stem.connection.get_protocolinfo  # only need the hash at launch
    # stem provides this via stem.process.launch_tor_with_config but we
    # use our own torrc; let's hash it manually via stem:


def _tor_hashed_password(password: str) -> str:
    """Return HashedControlPassword string for the torrc."""
    if _STEM_AVAILABLE:
        try:
            from stem.descriptor.remote import get_server_descriptors  # noqa: F401
        except Exception:
            pass
        # stem.connection.get_protocolinfo doesn't hash; use subprocess instead
    # fallback: use tor --hash-password
    try:
        result = subprocess.run(
            [cfg.tor_binary, "--hash-password", password],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("16:")]
        if lines:
            return lines[0]
    except Exception as exc:
        logger.warning("Could not hash tor password via binary: %s", exc)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  TorController
# ─────────────────────────────────────────────────────────────────────────────

class TorController:
    """
    Launches and manages *cfg.num_circuits* independent Tor processes.

    Each process gets:
      - SOCKSPort  = cfg.socks_base_port  + index*2
      - ControlPort= cfg.control_base_port + index*2
      - DataDirectory = cfg.tor_data_dir/instance-{index}
    """

    def __init__(self) -> None:
        self._circuits: List[Circuit] = []
        self._processes: Dict[int, subprocess.Popen] = {}   # index → Popen
        self._stem_controllers: Dict[int, object] = {}       # index → stem.Controller
        self._data_root = Path(cfg.tor_data_dir)
        self._torrc_template = Path(cfg.torrc_template)
        self._hashed_pw: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def circuits(self) -> List[Circuit]:
        return self._circuits

    @property
    def available_circuits(self) -> List[Circuit]:
        return [c for c in self._circuits if c.is_available]

    async def start(self) -> None:
        """Launch all Tor instances and wait until at least one bootstraps."""
        logger.info("Starting %d Tor instance(s)…", cfg.num_circuits)
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._hashed_pw = _tor_hashed_password(cfg.control_password)

        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(None, self._launch_instance, i)
            for i in range(cfg.num_circuits)
        ]
        await asyncio.gather(*tasks)

        # Build Circuit objects
        for i in range(cfg.num_circuits):
            c = Circuit(i, cfg.socks_port(i), cfg.control_port(i))
            self._circuits.append(c)

        # Give Tor a moment to open its control port before we connect
        logger.info("Waiting 3 s for Tor control ports to open…")
        await asyncio.sleep(3)

        # Wait until at least 1 circuit is ready, then start serving.
        # Remaining circuits continue bootstrapping in the background.
        await self._wait_for_bootstrap(min_ready=1)
        ready = len(self.available_circuits)
        if ready == 0:
            raise RuntimeError("No Tor circuits became ready. Check tor_data/ logs.")
        logger.info("%d/%d circuit(s) ready — starting proxy.", ready, cfg.num_circuits)

    async def stop(self) -> None:
        """Terminate all Tor processes."""
        logger.info("Stopping Tor instances…")
        for ctrl in self._stem_controllers.values():
            try:
                ctrl.close()
            except Exception:
                pass
        for proc in self._processes.values():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        self._processes.clear()
        self._stem_controllers.clear()
        logger.info("All Tor instances stopped.")

    async def new_identity(self, circuit: Circuit) -> bool:
        """
        Send NEWNYM to *circuit*, requesting a fresh Tor circuit.
        Returns True on success.
        """
        if not _STEM_AVAILABLE:
            logger.warning("[%s] stem not available — cannot rotate identity.", circuit.name)
            return False
        ctrl = self._stem_controllers.get(circuit.index)
        if ctrl is None:
            logger.warning("[%s] No stem controller — cannot rotate identity.", circuit.name)
            return False
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._send_newnym, ctrl, circuit)
            logger.info("[%s] NEWNYM sent — new Tor circuit established.", circuit.name)
            circuit.state = CircuitState.READY
            return True
        except Exception as exc:
            logger.error("[%s] NEWNYM failed: %s", circuit.name, exc)
            circuit.mark_dead(str(exc))
            return False

    async def new_identity_all(self) -> None:
        """Rotate identity on every circuit simultaneously."""
        tasks = [self.new_identity(c) for c in self._circuits]
        await asyncio.gather(*tasks)

    def get_circuit_stats(self) -> list:
        return [c.to_dict() for c in self._circuits]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_torrc(self, index: int) -> Path:
        """Render torrc.template for instance *index* and write it to disk."""
        data_dir = self._data_root / f"instance-{index}"
        data_dir.mkdir(parents=True, exist_ok=True)

        socks_port   = cfg.socks_port(index)
        control_port = cfg.control_port(index)

        template_text = ""
        if self._torrc_template.exists():
            template_text = self._torrc_template.read_text()
        else:
            # Minimal built-in template
            template_text = _BUILTIN_TORRC_TEMPLATE

        # Resolve tor binary directory (for GeoIP files bundled alongside tor.exe)
        tor_bin_dir = str(Path(cfg.tor_binary).resolve().parent)

        rendered = Template(template_text).substitute(
            SOCKS_PORT=socks_port,
            CONTROL_PORT=control_port,
            DATA_DIR=str(data_dir),
            HASHED_PASSWORD=self._hashed_pw,
            LOG_LEVEL="notice",
            TOR_BIN_DIR=tor_bin_dir,
        )
        torrc_path = data_dir / "torrc"
        torrc_path.write_text(rendered)
        return torrc_path

    def _launch_instance(self, index: int) -> None:
        """Launch one tor subprocess (blocking; run in executor)."""
        torrc_path = self._build_torrc(index)
        cmd = [cfg.tor_binary, "-f", str(torrc_path)]
        logger.debug("Launching: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._processes[index] = proc
            logger.info("Tor instance %d launched (pid=%d)", index, proc.pid)
        except FileNotFoundError:
            raise RuntimeError(
                f"Tor binary not found at '{cfg.tor_binary}'. "
                "Install Tor and ensure it is on PATH (or set TOR_BINARY env var)."
            )

    async def _wait_for_bootstrap(self, timeout: float = 120.0, min_ready: int = 1) -> None:
        """
        Bootstrap all Tor circuits.

        Returns as soon as *min_ready* circuits have reached PROGRESS=100,
        leaving any remaining circuits to continue bootstrapping in background
        asyncio tasks.

        IMPORTANT: We connect to stem ONCE per circuit and keep the controller
        alive across all poll iterations.  Closing the controller (ctrl.close())
        tells Tor that its owning process has disconnected, which causes Tor to
        shut itself down — producing the [WinError 10038] socket errors.
        """
        deadline = time.monotonic() + timeout
        loop = asyncio.get_event_loop()
        ready_event = asyncio.Event()   # fired when min_ready threshold is hit
        ready_count = 0

        async def _poll_one(circuit: Circuit) -> None:
            nonlocal ready_count
            ctrl = None
            while time.monotonic() < deadline:
                try:
                    if not _STEM_AVAILABLE:
                        # Without stem: TCP check on SOCKS port
                        import socket as _socket
                        try:
                            s = _socket.create_connection(("127.0.0.1", circuit.socks_port), timeout=3)
                            s.close()
                            circuit.mark_ready()
                            ready_count += 1
                            if ready_count >= min_ready:
                                ready_event.set()
                            return
                        except OSError:
                            pass
                    else:
                        # Connect once and reuse the controller
                        if ctrl is None:
                            ctrl = await loop.run_in_executor(
                                None, self._connect_controller, circuit
                            )
                            if ctrl is not None:
                                self._stem_controllers[circuit.index] = ctrl
                                logger.debug(
                                    "[%s] stem controller connected on port %d",
                                    circuit.name, circuit.control_port,
                                )

                        if ctrl is not None:
                            bootstrap = await loop.run_in_executor(
                                None, ctrl.get_info, "status/bootstrap-phase", ""
                            )
                            logger.debug("[%s] bootstrap-phase: %s", circuit.name, bootstrap)
                            if "PROGRESS=100" in bootstrap:
                                circuit.mark_ready()
                                ready_count += 1
                                if ready_count >= min_ready:
                                    ready_event.set()
                                return
                except Exception as exc:
                    logger.debug("[%s] Bootstrap poll error: %s", circuit.name, exc)
                    ctrl = None
                    self._stem_controllers.pop(circuit.index, None)

                await asyncio.sleep(2)

            circuit.mark_dead("Bootstrap timeout (120 s)")

        # Launch all bootstrap polls
        all_tasks = [asyncio.create_task(_poll_one(c)) for c in self._circuits]
        gather_future = asyncio.gather(*all_tasks, return_exceptions=True)

        # Wait until min_ready circuits are up OR all tasks finish
        wait_ready = asyncio.create_task(ready_event.wait())
        done, _    = await asyncio.wait(
            {wait_ready, *all_tasks}, return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel the "wait for ready" helper (not the poll tasks — let them run)
        wait_ready.cancel()
        still_running = [t for t in all_tasks if not t.done()]
        if still_running:
            logger.info(
                "%d/%d circuit(s) ready so far — %d still bootstrapping in background.",
                ready_count, len(self._circuits), len(still_running),
            )

    def _connect_controller(self, circuit: Circuit):
        """
        Open a stem Controller connection without closing it.
        Returns the controller on success, None on failure.
        """
        if not _STEM_AVAILABLE:
            return None
        try:
            ctrl = stem.control.Controller.from_port(port=circuit.control_port)
            ctrl.authenticate(password=cfg.control_password)
            return ctrl
        except Exception as exc:
            logger.debug("[%s] stem connect failed: %s", circuit.name, exc)
            return None

    @staticmethod
    def _send_newnym(ctrl, circuit: Circuit) -> None:
        import stem
        ctrl.signal(stem.Signal.NEWNYM)
        # Tor enforces a 10 s minimum between NEWNYMs
        time.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
#  Built-in minimal torrc template (used when torrc.template is missing)
# ─────────────────────────────────────────────────────────────────────────────

_BUILTIN_TORRC_TEMPLATE = """\
SOCKSPort $SOCKS_PORT
ControlPort $CONTROL_PORT
DataDirectory $DATA_DIR
HashedControlPassword $HASHED_PASSWORD
Log $LOG_LEVEL stdout
GeoIPFile $TOR_BIN_DIR/geoip
GeoIPv6File $TOR_BIN_DIR/geoip6
StrictNodes 0
ExitRelay 0
"""
