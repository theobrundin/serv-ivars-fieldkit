"""Background heartbeat thread. Runs independently of the detection loop so
camera/inference problems don't kill heartbeats (and vice versa).
"""
import threading
import time
import hashlib
from pathlib import Path

from . import __version__
from .config import Config
from .log import get
from .sender import Sender
from .telemetry import (
    DetectionCounters,
    cellular_status,
    system_stats,
    tailscale_status,
)

log = get(__name__)
DEPLOYED_SHA_PATH = Path("/home/serv/plate_reader/.deployed_sha")


def _build_provenance() -> dict[str, str]:
    """Return non-secret identifiers for the exact deployed source tree."""
    result: dict[str, str] = {}
    try:
        value = DEPLOYED_SHA_PATH.read_text(encoding="ascii").strip().lower()
        if len(value) == 40 and all(ch in "0123456789abcdef" for ch in value):
            result["build_git_sha"] = value
    except (OSError, UnicodeError):
        pass

    try:
        digest = hashlib.sha256()
        package_dir = Path(__file__).resolve().parent
        for path in sorted(package_dir.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        result["runtime_code_sha256"] = digest.hexdigest()
    except OSError:
        pass
    return result


class Heartbeat(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        sender: Sender,
        counters: DetectionCounters,
    ) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self.cfg = cfg
        self.sender = sender
        self.counters = counters
        # Do not call this ``_stop``: threading.Thread owns a private _stop()
        # method that join() invokes. Replacing it with an Event makes every
        # graceful shutdown fail with ``TypeError: 'Event' object is not
        # callable`` before the camera and SDK can be closed.
        self._stop_event = threading.Event()

    def _build_payload(self) -> dict:
        payload: dict = {
            "sw_version": __version__,
        }
        payload.update(_build_provenance())
        payload.update(system_stats())

        if self.cfg.telemetry.include_tailscale:
            ts = tailscale_status()
            if ts:
                payload["tailscale"] = ts

        if self.cfg.telemetry.include_cellular:
            cell = cellular_status()
            if cell:
                payload["cellular"] = cell

        try:
            payload["delivery_queue"] = self.sender.queue_stats()
        except Exception as e:
            log.debug("could not collect delivery queue stats: %s", e)

        # Drain counters only after slower best-effort collectors finish. If a
        # subprocess fails unexpectedly before this point, detection metrics
        # remain available for the next heartbeat instead of being lost.
        payload.update(self.counters.snapshot_and_reset())

        return payload

    def run(self) -> None:
        log.info("heartbeat thread started; interval=%ds",
                 self.cfg.behavior.heartbeat_interval_seconds)
        # Send one immediately at startup so we know the unit is alive
        try:
            self.sender.send_heartbeat(self._build_payload())
        except Exception as e:
            log.warning("initial heartbeat failed: %s", e)

        while not self._stop_event.is_set():
            # Wait first, then send. .wait() returns True if stopped.
            if self._stop_event.wait(
                self.cfg.behavior.heartbeat_interval_seconds
            ):
                break
            try:
                self.sender.send_heartbeat(self._build_payload())
            except Exception as e:
                log.exception("heartbeat send error: %s", e)
        log.info("heartbeat thread stopped")

    def stop(self) -> None:
        self._stop_event.set()
