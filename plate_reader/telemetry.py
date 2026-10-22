"""Telemetry collectors. Each function returns a dict (possibly empty on failure).
Everything in here is best-effort: a missing tailscale or qmicli should never
crash the heartbeat. Logging is at debug level for failures so we don't fill
the log with noise from a unit that's intentionally not on cellular yet.
"""
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Iterable

from .log import get

log = get(__name__)
_PSUTIL_PROCESS = None


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run a command, return stdout on success, None on any failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        if out.returncode != 0:
            log.debug("cmd %s failed rc=%d stderr=%s", cmd[0], out.returncode, out.stderr.strip()[:200])
            return None
        return out.stdout
    except FileNotFoundError:
        log.debug("cmd %s not found on PATH", cmd[0])
        return None
    except subprocess.TimeoutExpired:
        log.warning("cmd %s timed out after %ss", cmd[0], timeout)
        return None
    except Exception as e:
        log.debug("cmd %s exception: %s", cmd[0], e)
        return None


# ---------- System (cheap, always works) ----------

def system_stats() -> dict:
    """Pi thermal/throttle state, process resources, disk, uptime and RAM."""
    out = {}
    # Firmware readings are the authoritative Pi values. Fall back to sysfs on
    # development machines or stripped images.
    temp_raw = _run(["vcgencmd", "measure_temp"], timeout=2.0)
    if temp_raw and "=" in temp_raw:
        try:
            out["cpu_temp"] = float(
                temp_raw.split("=", 1)[1].split("'", 1)[0]
            )
        except (IndexError, ValueError):
            pass
    try:
        if "cpu_temp" not in out:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                out["cpu_temp"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass
    throttled = _run(["vcgencmd", "get_throttled"], timeout=2.0)
    if throttled and "0x" in throttled:
        try:
            bits = int(throttled.strip().split("0x", 1)[1], 16)
            out["throttled_hex"] = f"0x{bits:x}"
            out["under_voltage_now"] = bool(bits & 0x1)
            out["frequency_capped_now"] = bool(bits & 0x2)
            out["throttled_now"] = bool(bits & 0x4)
            out["soft_temp_limit_now"] = bool(bits & 0x8)
            out["throttling_has_occurred"] = bool(bits & 0x40000)
        except (IndexError, ValueError):
            pass
    clock = _run(["vcgencmd", "measure_clock", "arm"], timeout=2.0)
    if clock and "=" in clock:
        try:
            out["arm_clock_mhz"] = round(
                int(clock.split("=", 1)[1].strip()) / 1_000_000.0, 1
            )
        except (IndexError, ValueError):
            pass
    try:
        import psutil
        global _PSUTIL_PROCESS
        disk = psutil.disk_usage("/")
        out["disk_free_pct"] = round(100 * disk.free / disk.total, 1)
        mem = psutil.virtual_memory()
        out["mem_free_pct"] = round(100 * mem.available / mem.total, 1)
        swap = psutil.swap_memory()
        out["swap_used_pct"] = round(swap.percent, 1)
        if _PSUTIL_PROCESS is None:
            _PSUTIL_PROCESS = psutil.Process()
        proc = _PSUTIL_PROCESS
        out["process_rss_mb"] = round(
            proc.memory_info().rss / (1024 * 1024), 1
        )
        out["process_cpu_pct"] = round(proc.cpu_percent(interval=None), 1)
        out["process_threads"] = proc.num_threads()
        out["cpu_per_core_pct"] = [
            round(value, 1) for value in psutil.cpu_percent(
                interval=None, percpu=True
            )
        ]
        disk_io = psutil.disk_io_counters()
        if disk_io is not None:
            out["disk_write_mb_total"] = round(
                disk_io.write_bytes / (1024 * 1024), 1
            )
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            out["uptime_s"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    try:
        # 1-minute load average
        load1, _, _ = __import__("os").getloadavg()
        out["load_1m"] = round(load1, 2)
    except Exception:
        pass
    return out


# ---------- Clock sync ----------

def clock_synced() -> bool | None:
    """Returns True if system clock is NTP-synced, False if not, None if unknown.
    The script uses this at startup to refuse to start with a bad clock."""
    out = _run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
    if out is None:
        return None
    return out.strip().lower() == "yes"


# ---------- Tailscale ----------

def tailscale_status() -> dict:
    """Snapshot of tailscale state. Empty dict if tailscale not installed."""
    if not shutil.which("tailscale"):
        return {}
    raw = _run(["tailscale", "status", "--json"])
    if not raw:
        return {"state": "Unreachable"}
    try:
        data = json.loads(raw)
        self_node = data.get("Self") or {}
        backend_state = data.get("BackendState", "Unknown")
        tailnet_ips = self_node.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in tailnet_ips if "." in ip), None)
        health = data.get("Health") or []
        return {
            "state": backend_state,                       # "Running" is healthy
            "ip": ipv4,
            "health_ok": len(health) == 0,                 # tailscale's own self-check
            "health_messages": health[:3] if health else [],
        }
    except json.JSONDecodeError as e:
        log.debug("tailscale status JSON decode failed: %s", e)
        return {"state": "ParseError"}


# ---------- Sixfab / cellular ----------

def _qmicli(mode: str) -> str | None:
    """Read a recent snapshot written by the isolated root collector."""
    if mode not in {"signal", "serving", "stats"}:
        return None
    path = Path(f"/run/serv/qmi-{mode}.txt")
    try:
        if time.time() - path.stat().st_mtime > 180:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def cellular_status() -> dict:
    """Best-effort cellular snapshot. Empty dict on any failure."""
    out: dict = {}

    # RSSI / RSRQ / RSRP / SNR — uses --nas-get-signal-info, NOT the older
    # --nas-get-signal-strength. The latter does not reliably emit an "RSSI:"
    # line on this modem's firmware, which silently left rssi_dbm unset.
    signal_raw = _qmicli("signal")
    if signal_raw:
        for line in signal_raw.splitlines():
            line = line.strip()
            if line.startswith("RSSI:"):
                out["rssi_dbm"] = _parse_metric(line)
            elif line.startswith("RSRQ:"):
                out["rsrq_db"] = _parse_metric(line)
            elif line.startswith("RSRP:"):
                out["rsrp_dbm"] = _parse_metric(line)
            elif line.startswith("SNR:"):
                out["snr_db"] = _parse_metric(line)

    # Serving system: operator name + active radio type.
    # Real output shape (confirmed against actual modem output):
    #   Current PLMN:
    #       MCC: '310'
    #       MNC: '260'
    #       Description: 'T-Mobile'
    #   Radio interfaces: '1'
    #       [0]: 'lte'
    # The old code searched for a line ("Current network operator name")
    # that never appears in this format, so operator/radio were never set.
    serving = _qmicli("serving")
    if serving:
        in_current_plmn = False
        expect_radio_value = False
        for raw_line in serving.splitlines():
            line = raw_line.strip()

            if line.startswith("Current PLMN:"):
                in_current_plmn = True
                continue
            if in_current_plmn and line.startswith("Description:"):
                try:
                    out["operator"] = line.split(":", 1)[1].strip().strip("'")
                except IndexError:
                    pass
                in_current_plmn = False  # only take the FIRST PLMN block —
                # "Full operator code info:" later in the output has no
                # Description line, so this guard is mostly a safety net.

            if line.startswith("Radio interfaces:"):
                expect_radio_value = True
                continue
            if expect_radio_value:
                if line.startswith("[0]:"):
                    try:
                        out["radio"] = line.split(":", 1)[1].strip().strip("'").upper()
                    except IndexError:
                        pass
                expect_radio_value = False

    # Packet statistics (best-effort; some firmwares lack this)
    pkt = _qmicli("stats")
    if pkt:
        for line in pkt.splitlines():
            line = line.strip().lower()
            if line.startswith("tx bytes ok:"):
                try:
                    out["bytes_sent"] = int(line.split(":")[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("rx bytes ok:"):
                try:
                    out["bytes_received"] = int(line.split(":")[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

    # Convert bytes to MB for readability if present
    if "bytes_sent" in out:
        out["bytes_sent_mb"] = round(out.pop("bytes_sent") / (1024 * 1024), 2)
    if "bytes_received" in out:
        out["bytes_received_mb"] = round(out.pop("bytes_received") / (1024 * 1024), 2)

    return out


def _parse_metric(line: str) -> int | float | None:
    """Parses a qmicli metric line like "RSSI: '-64 dBm'" or "SNR: '7.8 dB'"
    into a numeric value. Returns None if the line doesn't match the expected
    quoted-value shape."""
    try:
        val = line.split("'")[1].split()[0]
        return float(val) if "." in val else int(val)
    except (IndexError, ValueError):
        return None


# ---------- Service-internal counters ----------

class DetectionCounters:
    """Thread-safe counters and per-heartbeat frame-latency distributions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.detections = 0
        self.successful_identifications = 0
        self.failed_no_plate = 0
        self.failed_low_confidence = 0
        self.failed_vote_disagreement = 0
        self.failed_group_floor = 0
        self.failed_below_threshold = 0
        self.failed_unreadable = 0
        self.drive_offs = 0
        # THE KPI: vehicles that went through the lane and were never
        # identified, so the FIFO queue cannot match them to an order.
        # main.py has always called record_failure("vehicle_only"), but this
        # class had no branch for it, so the count silently vanished and never
        # reached the heartbeat. Counted explicitly now.
        self.vehicle_only = 0
        # Of those, the ones where NO plate box was ever localized. Measured
        # on dtfootagefinal (5357 frames): all 4 vehicle-only events that DID
        # have a plate box also produced a drive-off within 40 frames, and all
        # 5 without one produced none. So vehicle_only and drive_offs overlap
        # precisely on had_plate_candidate, and summing them raw double-counts
        # those cars. This subset is disjoint from drive_offs by construction.
        self.vehicle_only_no_plate_box = 0
        self.inference_ms_sum = 0
        self.inference_count = 0
        # Normally drained every heartbeat (~600 samples at 5 fps). maxlen is
        # a failure-containment guard if the heartbeat thread ever stops.
        self.capture_ms: deque[float] = deque(maxlen=5000)
        self.pipeline_ms: deque[float] = deque(maxlen=5000)
        self.loop_ms: deque[float] = deque(maxlen=5000)

    def record_detection_attempt(self) -> None:
        with self._lock:
            self.detections += 1

    def record_success(self, inference_ms: int) -> None:
        with self._lock:
            self.successful_identifications += 1
            self.inference_ms_sum += inference_ms
            self.inference_count += 1

    def record_frame_timing(
        self,
        capture_ms: float,
        pipeline_ms: float,
        loop_ms: float,
    ) -> None:
        with self._lock:
            self.capture_ms.append(capture_ms)
            self.pipeline_ms.append(pipeline_ms)
            self.loop_ms.append(loop_ms)

    def record_failure(self, outcome: str) -> None:
        with self._lock:
            self._record_failure_unlocked(outcome)

    def _record_failure_unlocked(self, outcome: str) -> None:
        if outcome == "no_plate_detected":
            self.failed_no_plate += 1
        elif outcome == "low_confidence":
            self.failed_low_confidence += 1
        elif outcome == "vote_disagreement":
            self.failed_vote_disagreement += 1
        elif outcome == "group_confidence_floor":
            # Enough frames agreed on an identical string; the group floor
            # rejected them. A rising count here means the threshold is
            # costing confirms — NOT that plate reads are getting worse.
            self.failed_group_floor += 1
        elif outcome == "below_threshold":
            self.failed_below_threshold += 1
        elif outcome == "unreadable":
            self.failed_unreadable += 1
        elif outcome == "drive_off":
            self.drive_offs += 1
        elif outcome == "vehicle_only":
            # Prefer record_vehicle_only(), which can keep the counts
            # disjoint. This branch assumes the worst (that a plate box was
            # seen, so the car may also be a drive-off) and therefore does
            # NOT add to the disjoint subset — better to undercount the KPI
            # than to report the same car twice.
            self.vehicle_only += 1
        else:
            # An outcome nobody counts is an outcome nobody notices. This is
            # exactly how "vehicle_only" went missing from the heartbeat.
            log.warning(
                "record_failure got unknown outcome %r — not counted in any "
                "heartbeat metric. Add a branch for it.", outcome,
            )

    def record_vehicle_only(self, had_plate_candidate: bool) -> None:
        """A vehicle went through and was never identified.

        `had_plate_candidate` decides whether this car is ALSO reachable via
        the drive-off path (which requires a localized plate box), and so
        whether it can be added to the disjoint total without double-counting.
        """
        with self._lock:
            self.vehicle_only += 1
            if not had_plate_candidate:
                self.vehicle_only_no_plate_box += 1

    @staticmethod
    def _summary(values: Iterable[float]) -> dict:
        if not values:
            return {}
        ordered = sorted(values)
        n = len(ordered)

        def pct(q: float) -> float:
            return ordered[min(n - 1, int(round((n - 1) * q)))]

        return {
            "mean": round(sum(ordered) / n, 1),
            "p50": round(pct(0.50), 1),
            "p95": round(pct(0.95), 1),
            "p99": round(pct(0.99), 1),
            "max": round(ordered[-1], 1),
            "count": n,
        }

    def snapshot_and_reset(self) -> dict:
        with self._lock:
            return self._snapshot_and_reset_unlocked()

    def _snapshot_and_reset_unlocked(self) -> dict:
        snap = {
            "detections_since_last": self.detections,
            "successful_identifications_since_last": self.successful_identifications,
            "failed_no_plate_since_last": self.failed_no_plate,
            "failed_low_confidence_since_last": self.failed_low_confidence,
            "failed_vote_disagreement_since_last": self.failed_vote_disagreement,
            # Agreed frames rejected by the group floor. Distinct from
            # the above: this is a THRESHOLD cost, not an OCR failure.
            "failed_group_floor_since_last": self.failed_group_floor,
            "failed_below_threshold_since_last": self.failed_below_threshold,
            "failed_unreadable_since_last": self.failed_unreadable,
            "drive_offs_since_last": self.drive_offs,
            # KPI: cars through the lane with no plate registered.
            "vehicle_only_since_last": self.vehicle_only,
            "vehicle_only_no_plate_box_since_last": (
                self.vehicle_only_no_plate_box
            ),
            # PresenceTracker is the production source of truth. The legacy
            # vehicle_only counters remain in the payload for dashboard
            # compatibility, but production no longer emits that second,
            # overlapping event path.
            "unidentified_vehicles_since_last": self.drive_offs,
        }
        if self.inference_count > 0:
            snap["avg_inference_ms_since_last"] = round(
                self.inference_ms_sum / self.inference_count
            )
        snap["capture_ms"] = self._summary(self.capture_ms)
        snap["pipeline_ms"] = self._summary(self.pipeline_ms)
        snap["loop_ms"] = self._summary(self.loop_ms)
        # reset
        self.detections = 0
        self.successful_identifications = 0
        self.failed_no_plate = 0
        self.failed_low_confidence = 0
        self.failed_vote_disagreement = 0
        self.failed_group_floor = 0
        self.failed_below_threshold = 0
        self.failed_unreadable = 0
        self.drive_offs = 0
        self.vehicle_only = 0
        self.vehicle_only_no_plate_box = 0
        self.inference_ms_sum = 0
        self.inference_count = 0
        self.capture_ms = deque(maxlen=5000)
        self.pipeline_ms = deque(maxlen=5000)
        self.loop_ms = deque(maxlen=5000)
        return snap
