"""Entry point for the plate reader service.

Lifecycle:
  1. Load and validate config (fail loud)
  2. Self-test the hasher (catch broken hashlib)
  3. Verify clock is NTP-synced (best-effort; warn if not)
  4. Open camera
  5. Save detection-zone preview image (so Erik can verify aim)
  6. Open detector (loads fast-alpr model — first run downloads)
  7. Start sender thread (drains the queue)
  8. Auth preflight: refuse to start the detection loop if cloud rejects key
  9. Start heartbeat thread
  10. Detection loop: capture → process → emit → enqueue
  11. On SIGTERM: stop heartbeat, stop detection loop, drain queue briefly, shut down

Signals: SIGTERM is the systemd stop signal. SIGINT lets you Ctrl+C in dev.
"""
import signal
import sys
import time
from pathlib import Path

from .camera import Camera, CameraError, NullCamera, save_zone_preview
from .config import ConfigError, load as load_config
from .detector import (
    ConfirmedIdentification,
    Detector,
    FailedIdentification,
)
from .hasher import hash_plate, self_test
from .heartbeat import Heartbeat
from .engines import EngineContractError, RedactedPlateTextError
from .log import get, setup as setup_logging
from .miss_diagnostics import (
    prune_miss_plates,
    remove_existing_vehicle_cases,
    save_unidentified_vehicle,
    tag_confirmed_traffic_decision,
    tag_existing_route_case,
    tag_existing_terminal_case,
)
from .presence import DriveOff, PresenceTracker
from .sender import Sender
from .telemetry import DetectionCounters, clock_synced

log = get(__name__)
ZONE_PREVIEW_PATH = Path("/home/serv/plate_reader/zone_preview.jpg")

# Engines that read the camera themselves rather than being handed frames.
# For these, this process must not open the sensor — see NullCamera.
ENGINES_WITH_OWN_CAPTURE = frozenset({"pr_stream"})


class App:
    def __init__(self) -> None:
        self.cfg = None
        self.camera: Camera | None = None
        self.detector: Detector | None = None
        self.sender: Sender | None = None
        self.heartbeat: Heartbeat | None = None
        self.presence: PresenceTracker | None = None
        self.counters = DetectionCounters()
        # A recognized plate is an attribute of a physical vehicle profile,
        # not proof that the vehicle used the drive-through. Authoritative
        # route gating holds the detector outcome here until the same profile
        # has a completed trajectory that qualifies or rejects it. A board
        # observation alone is provisional because the car may exit top-right.
        self._deferred_identifications: dict[str, tuple[object, float]] = {}
        # Recognition failures use the same physical-route authority. A weak
        # plate on upper-right pass-through traffic is useful local evidence,
        # but it is not a drive-through miss and must not enter the cloud KPI.
        self._deferred_failures: dict[str, tuple[object, float]] = {}
        self._stop = False

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("received signal %d; shutting down", signum)
            self._stop = True
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    # ---------- Boot ----------

    def boot(self) -> None:
        setup_logging("INFO")
        log.info("=" * 60)
        log.info("serv-pi starting")

        try:
            self.cfg = load_config()
        except ConfigError as e:
            log.error("CONFIG ERROR: %s", e)
            log.error("Edit /home/serv/plate_reader/config.json and restart the service.")
            sys.exit(2)

        # Hasher self-test before we go any further
        try:
            self_test()
            log.info("hasher self-test passed")
        except Exception as e:
            log.error("HASHER SELF-TEST FAILED: %s", e)
            log.error("This is catastrophic — refusing to start to avoid corrupting data.")
            sys.exit(3)

        # Clock check (warn, don't fail)
        synced = clock_synced()
        if synced is True:
            log.info("system clock is NTP-synced")
        elif synced is False:
            log.warning(
                "system clock is NOT NTP-synced. Timestamps may be inaccurate. "
                "Continuing; the unit may have just booted and NTP hasn't run yet."
            )
        else:
            log.info("clock sync status unknown (timedatectl not available)")

        # Camera. Engines that pull frames themselves (Plate Recognizer
        # Stream reads an RTSP feed served by MediaMTX) must not open
        # Picamera2 here: libcamera gives the sensor to a single consumer,
        # so competing for it would starve the feed the engine depends on.
        engine_owns_capture = self.cfg.alpr.engine in ENGINES_WITH_OWN_CAPTURE
        try:
            if engine_owns_capture:
                self.camera = NullCamera(self.cfg.capture)
            else:
                self.camera = Camera(self.cfg.camera, self.cfg.capture)
            self.camera.open()
        except CameraError as e:
            log.error("CAMERA INIT FAILED: %s", e)
            sys.exit(4)

        # Capture one frame and dump a zone preview so Erik can verify aim.
        # Meaningless without a real sensor — a black frame would overwrite a
        # good preview with a useless one, so skip it in that case.
        if self.cfg.telemetry.save_zone_preview_on_boot and not engine_owns_capture:
            try:
                frame = self.camera.capture()
                save_zone_preview(
                    frame,
                    self.cfg.detection_zone,
                    ZONE_PREVIEW_PATH,
                    roi=self.cfg.alpr.roi,
                    dwell_zone=self.cfg.dwell_zone,
                    overlay=self.cfg.diagnostics.zone_preview_overlay,
                )
            except Exception as e:
                log.warning("zone preview capture failed: %s", e)

        # Detector (this loads the alpr model — slowest part of boot)
        try:
            self.detector = Detector(
                self.cfg, enable_vehicle_only_tracking=False
            )
            self.detector.open()
        except Exception as e:
            log.error("DETECTOR INIT FAILED: %s", e)
            sys.exit(5)

        # Presence tracker (drive-off / non-identified vehicle detection).
        self.presence = PresenceTracker(self.cfg)
        if self.cfg.presence.enabled:
            log.info(
                "presence tracking ENABLED (timeout=%.1fs, min_frames=%d, "
                "shadow_only=%s)",
                self.cfg.presence.track_timeout_seconds,
                self.cfg.presence.min_track_frames,
                getattr(self.cfg.presence, "shadow_only", False),
            )
        else:
            log.info("presence tracking disabled by config")

        # Sender + auth preflight
        self.sender = Sender(self.cfg)
        if not self.sender.auth_preflight():
            log.error(
                "AUTH PREFLIGHT FAILED. Refusing to start detection loop. "
                "Fix the api_key in config.json or the Devices table, then restart."
            )
            sys.exit(6)
        self.sender.start()

        # Heartbeat thread
        self.heartbeat = Heartbeat(self.cfg, self.sender, self.counters)
        self.heartbeat.start()

        log.info("boot complete; entering detection loop")

    # ---------- Detection loop ----------

    def run(self) -> None:
        self._install_signal_handlers()
        self.boot()

        assert self.camera and self.detector and self.sender
        # capture.target_fps is the sole pacing authority for new configs.
        # The legacy behavior.capture_interval_ms remains load-compatible but
        # no longer competes with the actual camera frame rate.
        cap_interval_s = 1.0 / self.cfg.capture.target_fps

        # Per-vehicle-hash cooldown to avoid spamming the cloud when one car
        # parks idling in the lane.
        cooldowns: dict[str, float] = {}
        cooldown_s = self.cfg.behavior.cooldown_seconds
        prune_interval = self.cfg.behavior.cooldown_prune_interval_seconds
        last_prune_mono = time.monotonic()

        frame_count = 0
        # Periodic achieved-fps log. Read-only observation of frame_count,
        # which already exists for other purposes — no detection/business
        # logic touched. The heartbeat reports detection counts to the cloud
        # every 120s but nothing previously logged an actual rate locally.
        fps_log_interval_s = 60.0
        fps_log_last_t = time.monotonic()
        fps_log_last_frame = 0
        healthy_frames = 0
        consecutive_pipeline_failures = 0
        pipeline_health_logged = False

        try:
            while not self._stop:
                loop_start_t = time.perf_counter()
                capture_start_t = time.perf_counter()
                try:
                    frame = self.camera.capture()
                except Exception as e:
                    healthy_frames = 0
                    consecutive_pipeline_failures += 1
                    log.warning("capture failed: %s", e)
                    if consecutive_pipeline_failures >= 25:
                        log.error(
                            "camera failed for 25 consecutive captures; "
                            "exiting so systemd can recover the pipeline"
                        )
                        break
                    time.sleep(0.2)
                    continue
                capture_ms = (time.perf_counter() - capture_start_t) * 1000.0

                frame_count += 1
                self.counters.record_detection_attempt()

                now_t = time.monotonic()
                if now_t - fps_log_last_t >= fps_log_interval_s:
                    achieved_fps = (frame_count - fps_log_last_frame) / (now_t - fps_log_last_t)
                    log.info(
                        "achieved %.2f fps over the last %.0fs (%d frames), "
                        "target %.2f fps",
                        achieved_fps, now_t - fps_log_last_t,
                        frame_count - fps_log_last_frame,
                        1.0 / cap_interval_s if cap_interval_s > 0 else float("inf"),
                    )
                    fps_log_last_t = now_t
                    fps_log_last_frame = frame_count

                pipeline_start_t = time.perf_counter()
                try:
                    candidates, outcomes = self.detector.process_frame(frame)
                    if not self.detector.last_inference_ok:
                        healthy_frames = 0
                        consecutive_pipeline_failures += 1
                        if consecutive_pipeline_failures >= 25:
                            log.error(
                                "inference failed for 25 consecutive frames; "
                                "exiting so systemd can recover the pipeline"
                            )
                            self._stop = True
                    # Feed presence with all localized boxes before handling
                    # identity outcomes. pr_stream owns capture, so its live
                    # vehicle observations come from the webhook bridge.
                    if self.presence is not None:
                        presence_candidates = list(candidates)
                        observation_drain = getattr(
                            self.detector.engine,
                            "drain_vehicle_observations",
                            None,
                        )
                        if callable(observation_drain):
                            presence_candidates.extend(observation_drain())
                        board_events = self.presence.observe(
                            frame, presence_candidates
                        )
                        # Board trigger: log-only for now. The ConverseNow
                        # signal wiring is a deliberate later decision —
                        # nothing is sent anywhere yet.
                        for ev in board_events or []:
                            log.info(
                                "BOARD_TRIGGER track=%d reason=%s "
                                "had_plate=%s qpos=%d depth=%d encounter=%s",
                                ev.track_id, ev.reason, ev.had_plate,
                                ev.queue_position, ev.queue_depth,
                                (ev.encounter_id or "")[:8],
                            )
                    for outcome in outcomes:
                        self._handle_outcome(outcome, cooldowns, cooldown_s)

                    # Resolve route-held identity outcomes while their owning
                    # encounter still exists. Terminal vehicle draining may
                    # remove a final rejected profile from the engine ledger.
                    self._drain_deferred_identifications(
                        cooldowns, cooldown_s
                    )
                    self._drain_deferred_failures()

                    # Vehicles the recogniser saw but read no plate for
                    # (Stream's detection_mode=vehicle). Counted as real
                    # vehicles, never as identities — they carry no plate and
                    # therefore no hash.
                    drain = getattr(self.detector.engine, "drain_vehicles", None)
                    if drain is not None:
                        for vehicle in drain():  # already coalesced per encounter
                            self._handle_plateless_vehicle(vehicle)

                    route_drain = getattr(
                        self.detector.engine,
                        "drain_route_diagnostics",
                        None,
                    )
                    if callable(route_drain):
                        for vehicle in route_drain():
                            persisted = self._handle_route_diagnostic(vehicle)
                            if persisted:
                                ack = getattr(
                                    self.detector.engine,
                                    "ack_route_diagnostic",
                                    None,
                                )
                                if callable(ack):
                                    ack(getattr(vehicle, "encounter_id", None))

                    # Close out any tracks that have gone quiet → drive-offs.
                    if self.presence is not None:
                        for drive_off in self.presence.reap():
                            if getattr(
                                self.cfg.presence, "shadow_only", False
                            ):
                                log.info(
                                    "SHADOW_DRIVE_OFF frames=%d dur=%.1fs "
                                    "had_plate=%s board=%s reason=%s",
                                    drive_off.frame_count,
                                    drive_off.duration_seconds,
                                    drive_off.had_plate_candidate,
                                    drive_off.passed_board,
                                    drive_off.end_reason,
                                )
                                continue
                            self._handle_drive_off(drive_off)
                            # Tell the detector another vehicle finished
                            # passing through. This is what lets a
                            # dwell-suppressed plate release early in a slow
                            # queue instead of waiting out the timer.
                            self.detector.note_vehicle_departed(None)

                    if self.detector.last_inference_ok:
                        consecutive_pipeline_failures = 0
                        healthy_frames += 1
                    if not pipeline_health_logged and healthy_frames >= 3:
                        log.info(
                            "FRAME_PIPELINE_HEALTHY: 3 successful camera + "
                            "inference frames"
                        )
                        pipeline_health_logged = True
                except (RedactedPlateTextError, EngineContractError):
                    # These are identity-safety failures, not bad frames. A
                    # retry loop would keep the service superficially alive
                    # while it can no longer produce trustworthy identities.
                    log.critical("fatal inference safety failure", exc_info=True)
                    raise
                except Exception as e:
                    healthy_frames = 0
                    consecutive_pipeline_failures += 1
                    log.exception("detector error on frame %d: %s", frame_count, e)
                    if consecutive_pipeline_failures >= 25:
                        log.error(
                            "pipeline raised for 25 consecutive frames; "
                            "exiting so systemd can recover it"
                        )
                        self._stop = True
                finally:
                    pipeline_ms = (
                        time.perf_counter() - pipeline_start_t
                    ) * 1000.0
                    loop_ms = (time.perf_counter() - loop_start_t) * 1000.0
                    self.counters.record_frame_timing(
                        capture_ms, pipeline_ms, loop_ms
                    )

                # Prune cooldown table periodically
                prune_now_mono = time.monotonic()
                if prune_now_mono - last_prune_mono > prune_interval:
                    cutoff = time.time() - 2 * cooldown_s
                    expired = [k for k, t in cooldowns.items() if t < cutoff]
                    for k in expired:
                        cooldowns.pop(k, None)
                    if expired:
                        log.debug("pruned %d expired cooldown entries", len(expired))
                    last_prune_mono = prune_now_mono

                # Periodic progress log. CPython releases each frame when its
                # reference drops; a forced full GC here caused avoidable
                # stop-the-world latency and did not address a measured leak.
                if frame_count % 1000 == 0:
                    del frame
                    log.info(
                        "frame %d; open votes=%d; queue=%d",
                        frame_count,
                        self.detector.open_vote_count(),
                        self.sender.queue_size(),
                    )

                # Sleep only the REMAINDER of the target interval, not the
                # full interval on top of however long this iteration already
                # took. The previous unconditional `time.sleep(cap_interval_s)`
                # was additive on top of capture + detect + presence + bookkeeping
                # every frame, so actual throughput was always below target —
                # measured on serv-ljs-001: target 5.00 fps, achieved ~3.75.
                # capture_interval_ms: 0 in config means no pacing at all —
                # max(0.0, 0 - elapsed) is always 0, so this needs no special
                # case; the loop simply runs as fast as capture+processing allow.
                elapsed_s = time.perf_counter() - loop_start_t
                sleep_s = max(0.0, cap_interval_s - elapsed_s)
                if sleep_s > 0:
                    time.sleep(sleep_s)
        finally:
            self.shutdown()

    def _within_open_hours(self) -> bool:
        """False outside configured hours; True if hours are not set."""
        return self.cfg.hours.is_open()

    def _cancel_deferred_failures_for_identity(
        self, engine, encounter_id: str | None
    ) -> int:
        """Remove unsent misses superseded by this physical identity.

        Route reconciliation can alias an early weak-localization profile to a
        later real plate profile.  The weak result must remain locally
        reviewable, but once the same canonical vehicle confirms it is not an
        identifiable miss and must never enter the KPI.
        """
        if not encounter_id or not self._deferred_failures:
            return 0
        canonical_fn = getattr(engine, "canonical_encounter_id", None)

        def canonical(value: str | None) -> str | None:
            if not value:
                return None
            if callable(canonical_fn):
                return canonical_fn(value) or value
            return value

        owner = canonical(encounter_id)
        removed = 0
        for key, (failure, _) in list(self._deferred_failures.items()):
            if canonical(getattr(failure, "encounter_id", None)) != owner:
                continue
            self._deferred_failures.pop(key, None)
            removed += 1
        if removed:
            log.info(
                "IDENTITY_RECONCILED encounter=%s cancelled_deferred_misses=%d",
                (owner or encounter_id)[:8],
                removed,
            )
        return removed

    def _record_confirmed_audit(
        self,
        outcome,
        decision: str,
        *,
        encounter_meta: dict | None = None,
        encounter_id: str | None = None,
    ) -> None:
        """Best-effort local provenance; never change an emission decision."""
        image_path = getattr(outcome, "diagnostic_image_path", None)
        if not image_path:
            return
        try:
            source_id = getattr(outcome, "encounter_id", None)
            engine = self.detector.engine if self.detector is not None else None
            metadata_fn = getattr(engine, "route_metadata", None)
            metadata = metadata_fn(source_id) if callable(metadata_fn) else None
            metadata = dict(metadata or {})
            if encounter_meta is not None:
                # The Visit uses the marker's snapshot. If a concurrent hook
                # changed its proof, keep that decision summary without
                # attaching a different trajectory as its explanation.
                if metadata.get("route_track_fingerprint") != encounter_meta.get(
                    "route_track_fingerprint"
                ):
                    metadata = {}
                metadata.update(encounter_meta)
            tag_confirmed_traffic_decision(
                image_path=image_path,
                recognition_encounter_id=source_id,
                encounter_id=(encounter_id or metadata.get("encounter_id") or source_id),
                decision=decision,
                route_metadata=metadata,
            )
        except Exception as e:
            # Diagnostic I/O or lookup failure must not suppress or retry a
            # real customer Visit, nor advance a pending/rejected one.
            log.warning("confirmed audit provenance unavailable: %s", e)

    def _handle_outcome(
        self,
        outcome,
        cooldowns: dict[str, float],
        cooldown_s: int,
    ) -> None:
        """Dispatch a Detector outcome: send /visit or /detection-event."""
        if isinstance(outcome, ConfirmedIdentification):
            # A per-frame threshold pass is only a candidate. The existing
            # Stream vehicle encounter becomes identified here, after the
            # detector's vote really confirms. This is the one physical-car
            # object shared by /visit and diagnostic evidence.
            encounter_meta = None
            engine = self.detector.engine if self.detector is not None else None
            terminal_emitted = getattr(
                engine, "terminal_visit_emitted", None
            )
            if (
                callable(terminal_emitted)
                and terminal_emitted(
                    getattr(outcome, "encounter_id", None)
                )
            ):
                # Check before mark_encounter_identified()/note_plate_read().
                # A cleaned-up terminal profile has no marker target; invoking
                # the legacy global plate latch here could suppress an
                # unrelated car that is currently in the lane.
                if self.presence is not None:
                    try:
                        self.presence.mark_identified(
                            outcome.normalized, outcome.vehicle_bbox
                        )
                    except Exception as e:
                        log.debug("mark_identified error: %s", e)
                dg = getattr(self.cfg, "diagnostics", None)
                outcome_encounter_id = (
                    getattr(outcome, "encounter_id", None) or ""
                )
                terminal_owner = getattr(
                    engine, "terminal_visit_owner", None
                )
                encounter_id = (
                    terminal_owner(outcome_encounter_id)
                    if callable(terminal_owner) else None
                ) or outcome_encounter_id
                self._record_confirmed_audit(
                    outcome, "suppressed_duplicate", encounter_id=encounter_id
                )
                removed_cases = 0
                if (
                    encounter_id
                    and bool(getattr(dg, "save_miss_plates", False))
                    and bool(getattr(
                        dg, "save_unidentified_vehicle_frames", False
                    ))
                ):
                    removed_cases = remove_existing_vehicle_cases(
                        directory=dg.miss_plate_dir,
                        encounter_id=encounter_id,
                        preserve_image_path=getattr(outcome, "diagnostic_image_path", None),
                    )
                log.info(
                    "SUPPRESSED late identity encounter=%s; shadow "
                    "anonymous Visit already emitted; removed_audit_cases=%d",
                    encounter_id[:8],
                    removed_cases,
                )
                return
            marker = getattr(engine, "mark_encounter_identified", None)
            if callable(marker):
                encounter_meta = marker(
                    getattr(outcome, "encounter_id", None),
                    outcome.normalized,
                )
            if encounter_meta is None and engine is not None:
                # Compatibility for a Stream read without a vehicle box.
                legacy_marker = getattr(engine, "note_plate_read", None)
                if callable(legacy_marker):
                    legacy_marker()

            self._cancel_deferred_failures_for_identity(
                engine, getattr(outcome, "encounter_id", None)
            )

            # Reconcile with presence tracking BEFORE anything else so an
            # identified car is never also reported as a drive-off.
            # NOTE: this runs even for dwell-suppressed re-reads — the car IS
            # still there and still identified, so it must not become a
            # drive-off just because we're not re-counting the visit.
            if self.presence is not None:
                try:
                    self.presence.mark_identified(
                        outcome.normalized, outcome.vehicle_bbox
                    )
                except Exception as e:
                    log.debug("mark_identified error: %s", e)

            # Dwell-suppressed re-read: the same car never left the frame, so
            # this is not a new visit. Emitted by the detector (rather than
            # dropped) so it stays visible here and in telemetry, but it must
            # not reach /visit. Distinct from the revisit cooldown below, which
            # governs how soon a genuinely RETURNING customer counts again.
            if outcome.suppressed:
                suppressed_hash = hash_plate(outcome.normalized)[:12]
                log.info(
                    "SUPPRESSED re-read hash=%s repeat=#%d (%s)",
                    suppressed_hash,
                    outcome.repeat_index,
                    outcome.suppress_reason,
                )
                self._record_confirmed_audit(
                    outcome, "suppressed_dwell", encounter_meta=encounter_meta
                )
                return

            if not self._within_open_hours():
                log.info(
                    "OUTSIDE_HOURS skip confirmed visit (%s %s–%s)",
                    self.cfg.hours.timezone,
                    self.cfg.hours.open,
                    self.cfg.hours.close,
                )
                self._record_confirmed_audit(
                    outcome, "outside_hours", encounter_meta=encounter_meta
                )
                return

            route_enabled = getattr(engine, "route_gate_enabled", None)
            route_shadow = getattr(engine, "route_gate_shadow_only", None)
            route_status = getattr(engine, "route_status", None)
            rejection_final = getattr(engine, "route_rejection_final", None)
            if (
                callable(route_enabled) and route_enabled()
                and not (callable(route_shadow) and route_shadow())
            ):
                status = (
                    route_status(getattr(outcome, "encounter_id", None))
                    if callable(route_status) else "pending"
                )
                rejected_in_grace = (
                    status == "rejected"
                    and callable(rejection_final)
                    and not rejection_final(
                        getattr(outcome, "encounter_id", None)
                    )
                )
                if status == "pending" or rejected_in_grace:
                    defer_key = (
                        getattr(outcome, "encounter_id", None)
                        or f"missing:{id(outcome)}"
                    )
                    if defer_key not in self._deferred_identifications:
                        self._deferred_identifications[defer_key] = (
                            outcome, time.monotonic()
                        )
                        log.info(
                            "ROUTE_PENDING encounter=%s recognized identity "
                            "held on-device status=%s",
                            defer_key[:8],
                            "rejection_grace" if rejected_in_grace else status,
                        )
                        self._record_confirmed_audit(
                            outcome, "pending_route", encounter_meta=encounter_meta
                        )
                    return
                if status == "rejected":
                    log.info(
                        "ROUTE_REJECTED encounter=%s recognized adjacent/"
                        "drive-off traffic excluded",
                        (getattr(outcome, "encounter_id", None) or "")[:8],
                    )
                    self._record_confirmed_audit(
                        outcome, "rejected_route", encounter_meta=encounter_meta
                    )
                    return

            try:
                vehicle_hash = hash_plate(outcome.normalized)
            except Exception as e:
                log.error("hash failed for confirmed plate: %s", e)
                self._record_confirmed_audit(
                    outcome, "identity_hash_error", encounter_meta=encounter_meta
                )
                return

            now = time.time()
            last_seen = cooldowns.get(vehicle_hash)
            if last_seen is not None and (now - last_seen) < cooldown_s:
                log.info(
                    "revisit cooldown skip hash=%s (%.0fs of %ds)",
                    vehicle_hash[:12], now - last_seen, cooldown_s,
                )
                self._record_confirmed_audit(
                    outcome, "suppressed_cooldown", encounter_meta=encounter_meta
                )
                return
            # A distinct customer means prior plates may return without waiting
            # out the full revisit window (lane cycling / short queue).
            stale = [h for h in cooldowns if h != vehicle_hash]
            for h in stale:
                del cooldowns[h]
            if stale:
                log.info(
                    "revisit cooldown reset for %d prior plate(s) after new visit",
                    len(stale),
                )
                if self.detector is not None:
                    self.detector.clear_confirm_cooldown_except(outcome.normalized)
            cooldowns[vehicle_hash] = now

            # Stream encounter metadata is the confidence-aggregated vehicle
            # profile. The detector vote sees only the confirming subset, so
            # it is a fallback for engines without an encounter ledger.
            if encounter_meta and (
                encounter_meta.get("make") or encounter_meta.get("model")
            ):
                profile_make = encounter_meta.get("make")
                profile_model = encounter_meta.get("model")
                make_confidence = encounter_meta.get("make_confidence")
                model_confidence = encounter_meta.get("model_confidence")
            else:
                profile_make = outcome.make
                profile_model = outcome.model
                make_confidence = getattr(
                    outcome, "make_confidence", None
                )
                model_confidence = getattr(
                    outcome, "model_confidence", None
                )
            if encounter_meta and encounter_meta.get("color"):
                profile_color = encounter_meta.get("color")
                color_confidence = encounter_meta.get("color_confidence")
            else:
                profile_color = outcome.color
                color_confidence = getattr(
                    outcome, "color_confidence", None
                )

            payload = {
                "licensePlate": vehicle_hash,  # field name is legacy; the value is a hash
                "visitId": (
                    encounter_meta.get("encounter_id")
                    if encounter_meta else getattr(outcome, "encounter_id", None)
                ),
                "encounterId": (
                    encounter_meta.get("encounter_id")
                    if encounter_meta else getattr(outcome, "encounter_id", None)
                ),
                "firstSeenAt": (
                    encounter_meta.get("first_seen_at")
                    if encounter_meta else outcome.detected_at_iso
                ),
                "lastSeenAt": (
                    encounter_meta.get("last_seen_at")
                    if encounter_meta else outcome.detected_at_iso
                ),
                "identifiedAt": outcome.detected_at_iso,
                # Vehicle-centric fields the /visit schema requires. Restored
                # after e96a40b reverted them; visit_schema.mjs falls back to
                # "recognized" when plateDetection is absent, so their loss was
                # silent — and make/model/color are exactly what MMC fills in.
                "vehicleDetected": True,
                "plateDetection": "recognized",
                "detectedAt": outcome.detected_at_iso,
                "frameCount": outcome.frame_count,
                "detectionConfidence": outcome.detection_confidence,
                "ocrConfidence": outcome.ocr_confidence,
                "inferenceDurationMs": outcome.inference_duration_ms,
                "detectionOutcome": "confirmed",
                # Descriptive only. /visit already accepts these (index.mjs
                # optionalString) and stores them on the Visits row; they are
                # never hashed, so a misclassification cannot split a customer.
                "color": profile_color,
                "make": profile_make,
                "model": profile_model,
                "colorConfidence": color_confidence,
                "makeConfidence": make_confidence,
                "modelConfidence": model_confidence,
                "vehicleType": (
                    encounter_meta.get("vehicle_type")
                    if encounter_meta else None
                ),
                "routeStatus": (
                    encounter_meta.get("route_status")
                    if encounter_meta else None
                ),
                "routeReason": (
                    encounter_meta.get("route_reason")
                    if encounter_meta else None
                ),
                "routeTrackFingerprint": (
                    encounter_meta.get("route_track_fingerprint")
                    if encounter_meta else None
                ),
                "trajectoryPoints": (
                    encounter_meta.get("trajectory_points")
                    if encounter_meta else None
                ),
                "dwellSeconds": (
                    encounter_meta.get("dwell_seconds")
                    if encounter_meta else None
                ),
                # This is the menu-board release time for route-authoritative
                # profiles. AWS already treats triggeredAt as optional.
                "triggeredAt": (
                    encounter_meta.get("route_qualified_at")
                    if encounter_meta else None
                ),
            }
            self.sender.send_visit(payload)
            self._record_confirmed_audit(
                outcome, "visit_queued", encounter_meta=encounter_meta
            )
            self.counters.record_success(outcome.inference_duration_ms)
            log.info(
                "CONFIRMED hash=%s det=%.2f ocr=%.2f frames=%d inference=%dms%s",
                vehicle_hash[:12],
                outcome.detection_confidence,
                outcome.ocr_confidence,
                outcome.frame_count,
                outcome.inference_duration_ms,
                (
                    f" crop={outcome.diagnostic_image_path}"
                    if getattr(outcome, "diagnostic_image_path", None) else ""
                ) + (
                    f" method={outcome.confirmation_method}"
                    if getattr(outcome, "confirmation_method", "normal_vote")
                    != "normal_vote" else ""
                ),
            )

        elif isinstance(outcome, FailedIdentification):
            engine = self.detector.engine if self.detector is not None else None
            outcome_encounter_id = (
                getattr(outcome, "encounter_id", None) or ""
            )
            diagnostic_encounter_id = outcome_encounter_id or None
            linker = getattr(engine, "note_detection_event", None)
            if callable(linker):
                linker(
                    getattr(outcome, "encounter_id", None),
                    outcome.episode_id,
                )
            terminal_emitted = getattr(
                engine, "terminal_visit_emitted", None
            )
            terminal_was_emitted = bool(
                callable(terminal_emitted)
                and terminal_emitted(
                    outcome_encounter_id
                )
            )
            if terminal_was_emitted:
                dg = getattr(self.cfg, "diagnostics", None)
                terminal_owner = getattr(
                    engine, "terminal_visit_owner", None
                )
                encounter_id = (
                    terminal_owner(outcome_encounter_id)
                    if callable(terminal_owner) else None
                ) or outcome_encounter_id
                diagnostic_encounter_id = encounter_id or None
                diagnostic_image_path = None
                if (
                    encounter_id
                    and bool(getattr(dg, "save_miss_plates", False))
                    and bool(getattr(
                        dg, "save_unidentified_vehicle_frames", False
                    ))
                ):
                    diagnostic_image_path = tag_existing_terminal_case(
                        directory=dg.miss_plate_dir,
                        encounter_id=encounter_id,
                        episode_id=outcome.episode_id,
                        outcome="insufficient_confidence",
                        plate_candidate=True,
                    )
                    if diagnostic_image_path is not None:
                        outcome.diagnostic_image_path = diagnostic_image_path
                log.info(
                    "LATE_MISS encounter=%s; shadow traffic Visit already "
                    "emitted, retaining recognition diagnostic%s",
                    encounter_id[:8],
                    (
                        f" frame={diagnostic_image_path}"
                        if diagnostic_image_path else ""
                    ),
                )
            if not self._within_open_hours():
                log.info(
                    "OUTSIDE_HOURS skip detection-event (%s %s–%s)",
                    self.cfg.hours.timezone,
                    self.cfg.hours.open,
                    self.cfg.hours.close,
                )
                return
            route_enabled = getattr(engine, "route_gate_enabled", None)
            route_shadow = getattr(engine, "route_gate_shadow_only", None)
            route_status = getattr(engine, "route_status", None)
            rejection_final = getattr(engine, "route_rejection_final", None)
            if (
                callable(route_enabled) and route_enabled()
                and not (callable(route_shadow) and route_shadow())
                and not terminal_was_emitted
            ):
                encounter_id = getattr(outcome, "encounter_id", None)
                status = (
                    route_status(encounter_id)
                    if callable(route_status) else "pending"
                )
                rejected_in_grace = (
                    status == "rejected"
                    and callable(rejection_final)
                    and not rejection_final(encounter_id)
                )
                if status == "pending" or rejected_in_grace:
                    defer_key = encounter_id or outcome.episode_id
                    if defer_key not in self._deferred_failures:
                        self._deferred_failures[defer_key] = (
                            outcome, time.monotonic()
                        )
                        log.info(
                            "ROUTE_PENDING encounter=%s recognition failure "
                            "held locally status=%s",
                            (defer_key or "")[:8],
                            "rejection_grace" if rejected_in_grace else status,
                        )
                    return
                if status == "rejected":
                    log.info(
                        "ROUTE_REJECTED encounter=%s recognition failure "
                        "excluded from KPI",
                        (encounter_id or "")[:8],
                    )
                    return
            payload = {
                "outcome": outcome.outcome,
                "detectedAt": outcome.detected_at_iso,
                "frameCount": outcome.frame_count,
                # DEPRECATED: independent maxima across the episode, so these
                # two routinely come from different frames. Kept because the
                # cloud schema still reads them. Prefer bestJoint* below.
                "topDetectionConfidence": outcome.top_detection_confidence,
                "topOcrConfidence": outcome.top_ocr_confidence,
                "inferenceDurationMs": outcome.inference_duration_ms,
                # --- additive; every consumer must tolerate their absence ---
                "bestJointDet": outcome.best_joint_det,
                "bestJointOcr": outcome.best_joint_ocr,
                "bestJointClearedDet": outcome.best_joint_cleared_det,
                "framesClearingDet": outcome.frames_clearing_det,
                "framesClearingOcr": outcome.frames_clearing_ocr,
                "framesClearingBoth": outcome.frames_clearing_both,
                "rejectReasonCounts": outcome.reject_reason_counts,
                "readsInEpisode": outcome.reads_in_episode,
                "framesAdmitted": outcome.frames_admitted,
                "maxBucketSize": outcome.max_bucket_size,
                "episodeId": outcome.episode_id,
                "episodeStart": outcome.episode_start,
                "episodeEnd": outcome.episode_end,
                "encounterId": diagnostic_encounter_id,
            }
            self.sender.send_detection_event(payload)
            self.counters.record_failure(outcome.outcome)
            log.info(
                "FAILED outcome=%s reads=%d admitted=%d best_joint=%.2f/%.2f"
                " (cleared_det=%s) episode=%s%s",
                outcome.outcome,
                outcome.reads_in_episode,
                outcome.frames_admitted,
                outcome.best_joint_det,
                outcome.best_joint_ocr,
                outcome.best_joint_cleared_det,
                outcome.episode_id[:8],
                (
                    f" crop={outcome.diagnostic_image_path}"
                    if outcome.diagnostic_image_path else ""
                ),
            )

    def _drain_deferred_failures(self) -> None:
        """Release only recognition failures owned by qualified lane traffic."""
        if not self._deferred_failures or self.detector is None:
            return
        engine = self.detector.engine
        status_fn = getattr(engine, "route_status", None)
        timeout_fn = getattr(engine, "route_decision_timeout_seconds", None)
        active_fn = getattr(engine, "route_profile_active", None)
        rejection_final_fn = getattr(engine, "route_rejection_final", None)
        timeout = timeout_fn() if callable(timeout_fn) else 120.0
        now = time.monotonic()
        for key, (outcome, started) in list(self._deferred_failures.items()):
            encounter_id = getattr(outcome, "encounter_id", None)
            status = (
                status_fn(encounter_id) if callable(status_fn) else "pending"
            )
            if status == "qualified":
                self._deferred_failures.pop(key, None)
                self._handle_outcome(outcome, {}, 0)
                continue
            if status == "rejected" and callable(rejection_final_fn):
                if not rejection_final_fn(encounter_id, now=now):
                    continue
            timed_out = (now - started) >= timeout
            if timed_out and callable(active_fn):
                timed_out = not active_fn(encounter_id, now=now)
            if status == "rejected" or timed_out:
                self._deferred_failures.pop(key, None)
                log.info(
                    "ROUTE_REJECTED encounter=%s recognition failure "
                    "excluded from KPI reason=%s",
                    (encounter_id or "")[:8],
                    "trajectory" if status == "rejected" else "decision_timeout",
                )

    def _drain_deferred_identifications(
        self,
        cooldowns: dict[str, float],
        cooldown_s: int,
    ) -> None:
        if not self._deferred_identifications or self.detector is None:
            return
        engine = self.detector.engine
        status_fn = getattr(engine, "route_status", None)
        timeout_fn = getattr(engine, "route_decision_timeout_seconds", None)
        active_fn = getattr(engine, "route_profile_active", None)
        rejection_final_fn = getattr(engine, "route_rejection_final", None)
        timeout = timeout_fn() if callable(timeout_fn) else 120.0
        now = time.monotonic()
        for key, (outcome, started) in list(
            self._deferred_identifications.items()
        ):
            encounter_id = getattr(outcome, "encounter_id", None)
            status = (
                status_fn(encounter_id) if callable(status_fn) else "pending"
            )
            if status == "qualified":
                self._deferred_identifications.pop(key, None)
                self._handle_outcome(outcome, cooldowns, cooldown_s)
                continue
            if status == "rejected" and callable(rejection_final_fn):
                if not rejection_final_fn(encounter_id, now=now):
                    continue
            timed_out = (now - started) >= timeout
            if timed_out and callable(active_fn):
                timed_out = not active_fn(encounter_id, now=now)
            if status == "rejected" or timed_out:
                self._deferred_identifications.pop(key, None)
                self._record_confirmed_audit(
                    outcome, "rejected_route" if status == "rejected" else "route_timeout"
                )
                log.info(
                    "ROUTE_REJECTED encounter=%s recognized traffic excluded "
                    "reason=%s",
                    (encounter_id or "")[:8],
                    "trajectory" if status == "rejected" else "decision_timeout",
                )

    def _handle_plateless_vehicle(self, vehicle) -> None:
        """A real vehicle the recogniser could not read a plate for.

        Posted to /visit as an anonymous vehicle, NOT to /detection-event.
        That is the whole point of running Stream in vehicle mode: a car with
        no readable plate is a genuine vehicle-level fact, not a recognition
        diagnostic, and it belongs in the same ledger that counts traffic.

        Deliberately carries no `licensePlate`: visit_schema.mjs rejects an
        unrecognized visit that includes one, and mints a retry-stable
        anonymous key from the Idempotency-Key instead. There is no plate
        here, so there is no hash and no customer identity — by construction.
        """
        if not self._within_open_hours():
            log.info(
                "OUTSIDE_HOURS skip vehicle_only (%s %s–%s)",
                self.cfg.hours.timezone,
                self.cfg.hours.open,
                self.cfg.hours.close,
            )
            return
        saw_plate = bool(getattr(vehicle, "saw_plate", False))
        plate_detection = (
            "insufficient_confidence" if saw_plate else "no_visible_plate"
        )
        encounter_id = getattr(vehicle, "encounter_id", None)
        diagnostic_image_path = None
        dg = getattr(self.cfg, "diagnostics", None)
        # A saw-plate encounter already persisted its best miss frame through
        # Detector. Save exactly one additional class here: vehicles for which
        # Stream never produced a plate candidate at all.
        if (
            not saw_plate
            and getattr(vehicle, "route_status", None) != "rejected"
            and bool(getattr(dg, "save_miss_plates", False))
            and bool(getattr(
                dg, "save_unidentified_vehicle_frames", False
            ))
        ):
            diagnostic_image_path = save_unidentified_vehicle(
                directory=dg.miss_plate_dir,
                jpeg=getattr(vehicle, "diagnostic_image_jpeg", None),
                encounter_id=encounter_id or "",
                outcome=plate_detection,
                vehicle_confidence=float(
                    getattr(vehicle, "vehicle_confidence", 0.0) or 0.0
                ),
                first_seen_at=getattr(vehicle, "first_seen_at", None) or "",
                last_seen_at=getattr(vehicle, "last_seen_at", None) or "",
                vehicle_type=getattr(vehicle, "vehicle_type", None),
                make=getattr(vehicle, "make", None),
                model=getattr(vehicle, "model", None),
                color=getattr(vehicle, "color", None),
                make_confidence=getattr(
                    vehicle, "make_confidence", None
                ),
                model_confidence=getattr(
                    vehicle, "model_confidence", None
                ),
                color_confidence=getattr(
                    vehicle, "color_confidence", None
                ),
                vehicle_bbox=(
                    getattr(vehicle, "diagnostic_image_bbox", None)
                    or getattr(vehicle, "vehicle_bbox", None)
                ),
                route_status=getattr(vehicle, "route_status", None),
                route_reason=getattr(vehicle, "route_reason", None),
                route_track_fingerprint=getattr(
                    vehicle, "route_track_fingerprint", None
                ),
                trajectory_points=getattr(
                    vehicle, "trajectory_points", 0
                ),
                dwell_seconds=getattr(vehicle, "dwell_seconds", None),
                route_trajectory_bboxes=getattr(
                    vehicle, "route_trajectory_bboxes", ()
                ),
                route_trajectory_timestamps=getattr(
                    vehicle, "route_trajectory_timestamps", ()
                ),
                route_entry_zone=getattr(
                    vehicle, "route_entry_zone", None
                ),
                route_board_zone=getattr(
                    vehicle, "route_board_zone", None
                ),
                route_terminal_exit_zone=getattr(
                    vehicle, "route_terminal_exit_zone", None
                ),
                diagnostic_image_observed_at=getattr(
                    vehicle, "diagnostic_image_observed_at", None
                ),
                plate_detection_confidence=getattr(
                    vehicle, "plate_detection_confidence", None
                ),
                plate_ocr_confidence=getattr(vehicle, "plate_ocr_confidence", None),
            )
            try:
                prune_miss_plates(
                    dg.miss_plate_dir,
                    dg.miss_plate_retain_hours,
                    getattr(dg, "miss_plate_max_cases", 0),
                )
            except Exception:
                pass
        payload = {
            "visitId": encounter_id,
            "encounterId": encounter_id,
            "vehicleDetected": True,
            "plateDetection": plate_detection,
            "detectedAt": getattr(vehicle, "first_seen_at", None),
            "firstSeenAt": getattr(vehicle, "first_seen_at", None),
            "lastSeenAt": getattr(vehicle, "last_seen_at", None),
            "detectionOutcome": plate_detection,
            "diagnosticEpisodeId": getattr(
                vehicle, "diagnostic_episode_id", None
            ),
            "color": getattr(vehicle, "color", None),
            "make": getattr(vehicle, "make", None),
            "model": getattr(vehicle, "model", None),
            "vehicleType": getattr(vehicle, "vehicle_type", None),
            "colorConfidence": getattr(
                vehicle, "color_confidence", None
            ),
            "makeConfidence": getattr(vehicle, "make_confidence", None),
            "modelConfidence": getattr(vehicle, "model_confidence", None),
            "routeStatus": getattr(vehicle, "route_status", None),
            "routeReason": getattr(vehicle, "route_reason", None),
            "routeTrackFingerprint": getattr(
                vehicle, "route_track_fingerprint", None
            ),
            "trajectoryPoints": getattr(vehicle, "trajectory_points", 0),
            "dwellSeconds": getattr(vehicle, "dwell_seconds", None),
            "detectionConfidence": (
                getattr(vehicle, "plate_detection_confidence", None)
                if saw_plate else getattr(vehicle, "vehicle_confidence", None)
            ),
            "ocrConfidence": getattr(vehicle, "plate_ocr_confidence", None),
        }
        self.sender.send_visit(payload)
        self.counters.record_failure("vehicle_only")
        log.info(
            "VEHICLE_TERMINAL encounter=%s outcome=%s type=%s color=%s "
            "make=%s conf=%.2f%s",
            (encounter_id or "")[:8], plate_detection,
            payload["vehicleType"], payload["color"], payload["make"],
            payload["detectionConfidence"] or 0.0,
            (
                f" frame={diagnostic_image_path}"
                if diagnostic_image_path else ""
            ),
        )

    def _handle_route_diagnostic(self, vehicle) -> bool:
        """Retain rejected route evidence locally without counting traffic."""
        classification = (
            getattr(vehicle, "traffic_classification", None)
            or "route_unresolved"
        )
        diagnostic_image_path = None
        dg = getattr(self.cfg, "diagnostics", None)
        if (
            bool(getattr(dg, "save_miss_plates", False))
            and bool(getattr(
                dg, "save_unidentified_vehicle_frames", False
            ))
        ):
            saw_plate = bool(getattr(vehicle, "saw_plate", False))
            episode_id = getattr(vehicle, "diagnostic_episode_id", None)
            encounter_id = getattr(vehicle, "encounter_id", None) or ""
            if episode_id or encounter_id:
                diagnostic_image_path = tag_existing_route_case(
                    directory=dg.miss_plate_dir,
                    episode_id=episode_id,
                    encounter_id=encounter_id,
                    traffic_classification=classification,
                    route_status="rejected",
                    route_reason=getattr(vehicle, "route_reason", None),
                    route_track_fingerprint=getattr(
                        vehicle, "route_track_fingerprint", None
                    ),
                    trajectory_points=getattr(
                        vehicle, "trajectory_points", 0
                    ),
                    dwell_seconds=getattr(vehicle, "dwell_seconds", None),
                    route_trajectory_bboxes=getattr(
                        vehicle, "route_trajectory_bboxes", ()
                    ),
                    route_trajectory_timestamps=getattr(
                        vehicle, "route_trajectory_timestamps", ()
                    ),
                    route_entry_zone=getattr(
                        vehicle, "route_entry_zone", None
                    ),
                    route_board_zone=getattr(
                        vehicle, "route_board_zone", None
                    ),
                    route_terminal_exit_zone=getattr(
                        vehicle, "route_terminal_exit_zone", None
                    ),
                    diagnostic_image_observed_at=getattr(
                        vehicle, "diagnostic_image_observed_at", None
                    ),
                    plate_candidate=saw_plate,
                )
            if diagnostic_image_path is None:
                diagnostic_image_path = save_unidentified_vehicle(
                    directory=dg.miss_plate_dir,
                    jpeg=getattr(vehicle, "diagnostic_image_jpeg", None),
                    encounter_id=(
                        getattr(vehicle, "encounter_id", None) or ""
                    ),
                    outcome=classification,
                    vehicle_confidence=float(
                        getattr(vehicle, "vehicle_confidence", 0.0) or 0.0
                    ),
                    first_seen_at=(
                        getattr(vehicle, "first_seen_at", None) or ""
                    ),
                    last_seen_at=(
                        getattr(vehicle, "last_seen_at", None) or ""
                    ),
                    vehicle_type=getattr(vehicle, "vehicle_type", None),
                    make=getattr(vehicle, "make", None),
                    model=getattr(vehicle, "model", None),
                    color=getattr(vehicle, "color", None),
                    make_confidence=getattr(
                        vehicle, "make_confidence", None
                    ),
                    model_confidence=getattr(
                        vehicle, "model_confidence", None
                    ),
                    color_confidence=getattr(
                        vehicle, "color_confidence", None
                    ),
                    vehicle_bbox=(
                        getattr(vehicle, "diagnostic_image_bbox", None)
                        or getattr(vehicle, "vehicle_bbox", None)
                    ),
                    route_status="rejected",
                    route_reason=getattr(vehicle, "route_reason", None),
                    route_track_fingerprint=getattr(
                        vehicle, "route_track_fingerprint", None
                    ),
                    trajectory_points=getattr(
                        vehicle, "trajectory_points", 0
                    ),
                    dwell_seconds=getattr(vehicle, "dwell_seconds", None),
                    route_trajectory_bboxes=getattr(
                        vehicle, "route_trajectory_bboxes", ()
                    ),
                    route_trajectory_timestamps=getattr(
                        vehicle, "route_trajectory_timestamps", ()
                    ),
                    route_entry_zone=getattr(
                        vehicle, "route_entry_zone", None
                    ),
                    route_board_zone=getattr(
                        vehicle, "route_board_zone", None
                    ),
                    route_terminal_exit_zone=getattr(
                        vehicle, "route_terminal_exit_zone", None
                    ),
                    diagnostic_image_observed_at=getattr(
                        vehicle, "diagnostic_image_observed_at", None
                    ),
                    traffic_classification=classification,
                    plate_candidate=saw_plate,
                    plate_detection_confidence=getattr(
                        vehicle, "plate_detection_confidence", None
                    ),
                    plate_ocr_confidence=getattr(vehicle, "plate_ocr_confidence", None),
                )
            try:
                prune_miss_plates(
                    dg.miss_plate_dir,
                    dg.miss_plate_retain_hours,
                    getattr(dg, "miss_plate_max_cases", 0),
                )
            except Exception:
                pass
        log.info(
            "ROUTE_DIAGNOSTIC encounter=%s class=%s reason=%s "
            "plate_candidate=%s%s",
            (getattr(vehicle, "encounter_id", None) or "")[:8],
            classification,
            getattr(vehicle, "route_reason", None),
            bool(getattr(vehicle, "saw_plate", False)),
            (
                f" frame={diagnostic_image_path}"
                if diagnostic_image_path else ""
            ),
        )
        diagnostics_enabled = bool(
            getattr(dg, "save_miss_plates", False)
        ) and bool(getattr(dg, "save_unidentified_vehicle_frames", False))
        if diagnostics_enabled and diagnostic_image_path is None:
            log.warning(
                "ROUTE_DIAGNOSTIC persistence deferred encounter=%s",
                (getattr(vehicle, "encounter_id", None) or "")[:8],
            )
            return False
        return True

    def _handle_drive_off(self, drive_off: DriveOff) -> None:
        """Send the authoritative unidentified-vehicle terminal event."""
        if not self._within_open_hours():
            log.info(
                "OUTSIDE_HOURS skip drive_off (%s %s–%s)",
                self.cfg.hours.timezone,
                self.cfg.hours.open,
                self.cfg.hours.close,
            )
            return
        payload = {
            "outcome": drive_off.outcome,               # 'drive_off'
            "detectedAt": drive_off.detected_at_iso,
            "frameCount": drive_off.frame_count,
            "topDetectionConfidence": drive_off.top_detection_confidence,
            "topOcrConfidence": drive_off.top_ocr_confidence,
            "inferenceDurationMs": drive_off.inference_duration_ms,
            # Extra context specific to drive-offs:
            "durationSeconds": drive_off.duration_seconds,
            "hadPlateCandidate": drive_off.had_plate_candidate,
            "dominantColor": drive_off.dominant_color,
            "splitFromColor": drive_off.split_from_color,
            "passedBoard": getattr(drive_off, "passed_board", False),
            "endReason": getattr(drive_off, "end_reason", "timeout"),
            # Episode identity, same contract as the detector's miss episodes.
            # getattr-guarded so a DriveOff from an older presence build still
            # sends rather than raising.
            "episodeId": getattr(drive_off, "episode_id", ""),
            "episodeStart": getattr(drive_off, "episode_start", ""),
            "episodeEnd": getattr(drive_off, "episode_end", ""),
        }
        self.sender.send_detection_event(payload)
        self.counters.record_failure("drive_off")
        log.info(
            "DRIVE_OFF sent frames=%d dur=%.1fs had_plate=%s top_det=%.2f",
            drive_off.frame_count,
            drive_off.duration_seconds,
            drive_off.had_plate_candidate,
            drive_off.top_detection_confidence,
        )

    # ---------- Shutdown ----------

    def shutdown(self) -> None:
        log.info("shutdown: stopping heartbeat...")
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat.join(timeout=3.0)

        log.info("shutdown: draining sender (up to 5s)...")
        # Give the sender a brief moment to flush in-flight stuff
        if self.sender:
            t0 = time.monotonic()
            while (self.sender.queue_size() > 0
                   and (time.monotonic() - t0) < 5.0):
                time.sleep(0.2)
            self.sender.stop()

        log.info("shutdown: closing camera...")
        if self.camera:
            self.camera.close()

        # Release the inference engine. No-op for fast-alpr, but ultimateALPR
        # holds a process-global engine whose deInit() must run — otherwise a
        # subsequent init() in the same process is undefined.
        log.info("shutdown: closing detector...")
        if self.detector:
            self.detector.close()

        log.info("shutdown complete")


def main() -> None:
    app = App()
    app.run()


if __name__ == "__main__":
    main()
