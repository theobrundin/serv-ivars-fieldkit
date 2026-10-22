"""Pluggable plate-inference engines behind a single Protocol.

This module owns EXACTLY ONE responsibility: turn a frame into a list of
`RawPlateRead`. It deliberately does NOT normalize, threshold, zone-test, or
vote. Those live in `detector.py` and must stay there — swapping the engine
must not be able to alter identity semantics.

Why the seam is here specifically
---------------------------------
Before this module existed, `fast_alpr` was referenced in exactly two places
repo-wide, and no `fast_alpr` type ever escaped `Detector._run_one()`; the
result objects were consumed via duck-typed `getattr`. So the engine boundary
was already implicit and narrow. This makes it explicit without widening it.

`RawPlateRead` is intentionally NOT a `DetectionResult`. A `DetectionResult`
has already been through `normalize()`, `bbox_inside_zone()`, and the identify
thresholds. Keeping the two types distinct is what guarantees a new engine
cannot smuggle a different normalization or a different confidence rule into
the hash path.

See docs/ULTIMATEALPR_SPIKE_FINDINGS.md §5.1 for the design rationale.
"""
import sys
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .log import get

log = get(__name__)


def _to_scalar_confidence(value) -> float:
    """fast-alpr's OCR confidence can be a single float (older API) or a
    list of per-character confidences (fast-plate-ocr's current API). Coerce
    either shape into one representative float.

    We take the MINIMUM across characters, not the mean. This is the single
    most important line in this file for identification correctness.

    Why: a plate string is only as trustworthy as its WEAKEST character. One
    misread character makes the whole string — and therefore the hash — wrong.
    Averaging hides exactly that. Measured on real footage (2026-07-25, full
    17:53 clip):

        sample frame  "TESTI234"  chars T:1.00 E:1.00 S:1.00 T:1.00
                                      I:0.79 <-- misread  2:1.00 3:0.98 4:0.94
                   mean = 0.971  -> PASSES a 0.85 floor, ships a wrong hash
                   min  = 0.794  -> correctly REJECTED

    The expected synthetic fixture is TEST1234. Under the old mean-based gate,
    the one-character misread would confirm and be sent as the identity.

    Taking the min turns per-character confidence into a genuine quality
    filter. Across the full clip it collapsed 67 distinct OCR strings to 19
    and left ZERO encounters where two different strings both survived — i.e.
    the "OCR flap" that motivated fuzzy vote-matching was, in its entirety,
    low-quality reads that should never have been trusted.

    Moved here from detector.py so engines can share it without a circular
    import. `detector.py` re-exports it; behaviour is unchanged.
    """
    if isinstance(value, (list, tuple)):
        return float(min(value)) if value else 0.0
    return float(value)


@dataclass(frozen=True)
class RawPlateRead:
    """One plate localized in one frame, in engine-neutral terms.

    Confidences are ALWAYS 0.0-1.0 here. Engines whose native scale differs
    (ultimateALPR returns 0-100) must convert before constructing this, so
    that `Thresholds` means the same thing regardless of engine.

    `bbox` is ALWAYS an axis-aligned (x1, y1, x2, y2) in pixels. Engines that
    return quads must reduce before constructing this.
    """
    text: str
    detection_confidence: float
    ocr_confidence: float
    bbox: tuple[int, int, int, int]
    # --- optional, engine-dependent; None on engines that don't provide them ---
    # Populated only by engines with vehicle detection. Unused by the identify
    # pipeline today; carried so Problem 2 work doesn't need another refactor.
    vehicle_bbox: tuple[int, int, int, int] | None = None
    vehicle_confidence: float | None = None
    # LPCI country/state, e.g. "USA/California". NEVER enters the hash input —
    # NORMALIZATION_SPEC v1.0 rule 8 is plate text only. Diagnostics only.
    jurisdiction: str | None = None
    # Make / model / colour, from engines with MMC classifiers enabled.
    # Descriptive attributes carried to /visit for staff context. Like
    # jurisdiction these NEVER enter the hash input — the identity is the
    # normalized plate and nothing else, so a misclassified colour can never
    # split or merge a customer.
    make: str | None = None
    model: str | None = None
    color: str | None = None
    # Confidence of the descriptive classifiers, when the engine exposes it.
    # Plate Recognizer supplies one score for the make/model pair and a
    # separate colour score.  These values enrich Visits only; they are never
    # identity or hash input.
    make_confidence: float | None = None
    model_confidence: float | None = None
    color_confidence: float | None = None
    # Stable physical-encounter identity assigned by engines that also see
    # the vehicle (currently Plate Recognizer Stream). It is correlation
    # metadata only: never normalized, hashed, or used as customer identity.
    encounter_id: str | None = None
    # True when Stream's bounded encounter settlement observed another
    # normalized string for this same vehicle. Identity-rescue paths may use
    # this to refuse an otherwise strong but disputed single result.
    had_competing_variants: bool = False
    # Capture timestamp supplied by the recognition provider. Stream results
    # can wait in two temporal selectors before detector.py sees them, so
    # stamping a drained batch with the local dequeue time loses the actual
    # evidence order. Engines without provider timing leave this unset.
    captured_at_iso: str | None = None
    # Stable identity of the provider observation that produced this read.
    # Several independently selected Stream observations may drain in one
    # infer() call; conversely, pyramidal/duplicate boxes from one observation
    # are still only one independent vote. Correlation metadata only.
    source_observation_id: str | None = None
    # Optional full-frame image from engines that already captured (Stream
    # webhook JPEG). Never leaves the device. Used only to persist the
    # highest-OCR frame of an episode; not hash input.
    image: object | None = field(default=None, repr=False, compare=False)


@runtime_checkable
class PlateEngine(Protocol):
    """The whole engine contract. Four members, nothing else."""

    @property
    def name(self) -> str:
        """Short stable identifier, used in logs and telemetry."""
        ...

    def open(self) -> None:
        """Load models / initialise the engine. May be slow."""
        ...

    def close(self) -> None:
        """Release the engine. Must be idempotent."""
        ...

    def infer(self, frame) -> list[RawPlateRead]:
        """Run inference on one frame. Must not raise on a bad frame —
        log and return [] instead, so one bad frame can't kill the loop."""
        ...


class FastAlprEngine:
    """Today's engine, extracted verbatim. No behaviour change.

    Everything here was lifted from `Detector.open()` and the inference half
    of `Detector._run_one()` as-is, including log message wording, the
    defensive `getattr` access, and the malformed-result skip.
    """

    name = "fast_alpr"

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg          # unused; kept so both engines share a shape
        self._alpr = None
        self.last_call_ok = False

    def open(self) -> None:
        try:
            from fast_alpr import ALPR
        except ImportError as e:
            raise RuntimeError(
                f"fast_alpr not installed: {e}. Run scripts/install.sh."
            ) from e

        log.info("loading fast-alpr model (first run may download weights)...")
        # On macOS (dev/harness only) onnxruntime advertises CoreML, which both
        # spams warnings and gives timings that mean nothing for the Pi. Pin to
        # CPU there. On the Pi we pass nothing and let onnxruntime choose, so
        # this file needs no edit before deploy — the previous hardcoded CPU
        # providers were a Mac-only line that had to be manually reverted, which
        # is exactly the kind of step that gets forgotten.
        #
        # Verified correctly guarded by the Stage 1 spike; do NOT "clean up".
        import platform
        if platform.system() == "Darwin":
            self._alpr = ALPR(
                detector_providers=["CPUExecutionProvider"],
                ocr_providers=["CPUExecutionProvider"],
            )
        else:
            self._alpr = ALPR()
        log.info("fast-alpr model loaded")

    def close(self) -> None:
        # fast-alpr has no teardown API; dropping the reference is all we can
        # do. Idempotent by construction.
        self._alpr = None

    def infer(self, frame) -> list[RawPlateRead]:
        if self._alpr is None:
            raise RuntimeError("Detector.open() not called")

        self.last_call_ok = False
        try:
            results = self._alpr.predict(frame)
        except Exception as e:
            log.warning("alpr.predict raised %s; skipping frame", e)
            return []

        self.last_call_ok = True
        out: list[RawPlateRead] = []
        for r in results or []:
            # fast-alpr's API gives us r.detection.confidence and r.ocr.confidence / text.
            # Defensive about absence of fields.
            try:
                det_conf = _to_scalar_confidence(getattr(r.detection, "confidence", 0.0))
                ocr_text = getattr(r.ocr, "text", "") or ""
                ocr_conf = _to_scalar_confidence(getattr(r.ocr, "confidence", 0.0))
                bbox_obj = getattr(r.detection, "bounding_box", None)
                if bbox_obj is not None:
                    bbox = (
                        int(getattr(bbox_obj, "x1", 0)),
                        int(getattr(bbox_obj, "y1", 0)),
                        int(getattr(bbox_obj, "x2", 0)),
                        int(getattr(bbox_obj, "y2", 0)),
                    )
                else:
                    bbox = (0, 0, 0, 0)
            except Exception as e:
                log.warning("malformed alpr result: %s", e)
                continue

            out.append(RawPlateRead(
                text=ocr_text,
                detection_confidence=det_conf,
                ocr_confidence=ocr_conf,
                bbox=bbox,
            ))
        return out


def _install_imp_shim() -> None:
    """Let the vendor's SWIG wrapper import on Python >= 3.12.

    `ultimateAlprSdk.py` was generated by SWIG 2.0.9 and starts with:

        def swig_import_helper():
            import imp                                    # <-- line 13
            try:
                fp, pathname, desc = imp.find_module('_ultimateAlprSdk', ...)
            except ImportError:
                import _ultimateAlprSdk                   # <-- the fallback
                return _ultimateAlprSdk

    `imp` was deprecated in 3.4 and REMOVED in 3.12, and the import sits
    outside the try, so on the Pi (Python 3.13) the wrapper dies with
    ModuleNotFoundError before reaching its own perfectly good fallback.

    Rather than patch vendor code — which would silently revert on the next
    SDK update — install a minimal stand-in whose `find_module` raises
    ImportError. That routes the wrapper down its documented fallback path,
    a plain `import _ultimateAlprSdk`, which works because PYTHONPATH already
    includes the binaries directory holding the .so.

    Scoped and conservative: no-op if a real `imp` exists (Python <= 3.11) or
    if something already registered one.
    """
    import sys
    if "imp" in sys.modules or sys.version_info < (3, 12):
        return
    import types
    shim = types.ModuleType("imp")
    shim.__doc__ = (
        "Minimal stand-in for the removed stdlib `imp`, installed by "
        "plate_reader.engines so the ultimateALPR SWIG wrapper can fall back "
        "to a direct import. Not a general-purpose implementation."
    )

    def find_module(name, path=None):
        raise ImportError(
            f"imp shim: no find_module support for {name!r}; "
            "use the direct-import fallback"
        )

    def load_module(name, fp, pathname, description):
        raise ImportError("imp shim: no load_module support")

    shim.find_module = find_module
    shim.load_module = load_module
    sys.modules["imp"] = shim
    log.debug("installed minimal `imp` shim for the SWIG wrapper (py>=3.12)")


class RedactedPlateTextError(RuntimeError):
    """Raised when the SDK returns a `*`-redacted plate string.

    Its own type so callers can catch it specifically rather than by message.
    """


class EngineContractError(RuntimeError):
    """The loaded SDK violates a confidence/layout safety assumption."""


# Module-level singleton guard. ultimateALPR's engine is PROCESS-global: init /
# process / deInit are free functions over one hidden instance, so two live
# engines in one interpreter is undefined behaviour rather than merely wasteful.
# The harness legitimately builds more than one Detector (A/B runs, sweeps), so
# this has to fail loudly instead of silently corrupting the second one.
_UALPR_LIVE: "UltimateAlprEngine | None" = None


class UltimateAlprEngine:
    """Doubango ultimateALPR, behind the same Protocol as fast-alpr.

    PLUMBING ONLY — no accuracy claims. In trial mode the SDK redacts the last
    character of every plate, so any read here is expected to trip the `*`
    guard. That is the correct outcome, not a bug.

    Three things this adapter must get right, all of them silent if wrong:

    1. **Confidence scale.** Config `*_minscore` keys are 0-1; returned
       confidences are 0-100. Mixing them makes every threshold meaningless.
    2. **`warpedBox` is a genuine quadrilateral** (8 floats), not two corners.
       Reduced here to an axis-aligned box.
    3. **`*` redaction must raise, never normalize.** normalize() would strip
       the `*` and the 5-6 char survivor clears the length floor, producing a
       confident wrong identity that nothing downstream can detect.
    """

    name = "ultimate_alpr"

    # confidences = [ OCR score, plate detection score, char[0], ... char[N] ]
    # Vendor-documented; matches the measured len(confidences) == len(text) + 2.
    _CONF_OCR = 0
    _CONF_DETECT = 1
    _CONF_CHARS_FROM = 2

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        self._sdk = None
        self._opened = False
        self.last_call_ok = False
        # One-shot validation that the binary matches the documented
        # confidences layout (see _validate_confidences_layout).
        self._layout_checked = False
        # One-shot proof that the licence actually loaded (see
        # verify_license_unmasked). None until checked.
        self._license_verified: bool | None = None
        # Real frame dimensions, needed to turn a normalized ROI into the
        # pixel detect_roi the SDK wants. Set via set_frame_size() before
        # open() when the caller knows them.
        self._frame_w = int(getattr(getattr(cfg, "capture", None), "width", 0) or 0)
        self._frame_h = int(getattr(getattr(cfg, "capture", None), "height", 0) or 0)

    # -- config ---------------------------------------------------------
    def _resolve_license_token_file(self) -> str | None:
        """Path to the licence token file, or None for trial mode.

        A FILE PATH, never the token itself. The token must not appear in
        config.json, in argv (visible in `ps` and shell history), in logs, or
        anywhere in the repo. The SDK reads the file itself via its
        `license_token_file` key.
        """
        # The alpr block is authoritative; the flat attribute is the legacy
        # location kept for pre-schema configs and test stubs. Reading only
        # the flat one silently drops the token when a new-schema config is
        # used, which degrades to trial mode with no error — exactly the
        # failure this guard exists to prevent.
        alpr = getattr(self._cfg, "alpr", None)
        path = getattr(alpr, "license_token_file", None) if alpr else None
        if not path:
            path = getattr(self._cfg, "license_token_file", None)
        if not path:
            return None
        from pathlib import Path
        p = Path(path).expanduser()
        if not p.is_file():
            # Fail loud. Falling through to trial mode here is the exact
            # silent-degradation this whole guard system exists to prevent:
            # the engine would run, redact every plate, and write wrong
            # identities without a single error.
            raise RuntimeError(
                f"license_token_file configured but not readable: {p}. "
                "Refusing to start — the SDK would silently fall back to "
                "trial mode and emit '*'-redacted plates that hash into "
                "confident wrong identities."
            )
        return str(p)

    def set_frame_size(self, width: int, height: int) -> None:
        """Tell the engine the real frame size so a normalized ROI can be
        converted to the pixel `detect_roi` the SDK expects.

        Config stores normalized coordinates precisely so this conversion
        happens exactly once, here, against the actual frame — see
        config.NormBox.
        """
        if (width, height) == (self._frame_w, self._frame_h):
            return
        self._frame_w, self._frame_h = width, height

    def _detect_roi_pixels(self) -> list:
        """Engine `detect_roi` as [left, right, top, bottom] pixels.

        Returns [0,0,0,0] (full frame) when the ROI is disabled — the exact
        value used before ROI support existed.
        """
        alpr = getattr(self._cfg, "alpr", None)
        roi = getattr(alpr, "roi", None) if alpr else None
        if roi is None or not roi.enabled:
            return [0, 0, 0, 0]
        if not self._frame_w or not self._frame_h:
            log.warning(
                "alpr.roi is enabled but the frame size is unknown at init; "
                "falling back to full frame for this session."
            )
            return [0, 0, 0, 0]
        x1, y1, x2, y2 = roi.to_pixels(self._frame_w, self._frame_h)
        # The SDK orders detect_roi as [left, right, top, bottom] — NOT
        # [x1, y1, x2, y2]. Getting this wrong silently searches the wrong
        # region, which looks exactly like "the model stopped working".
        #
        # VERIFIED against the SDK, two independent sources:
        #   samples/java/recognizer/Recognizer.java:167
        #       "pattern: [left, right, top, bottom]"
        #   validation strings inside libultimate_alpr-sdk.so, in this order:
        #       "roi.left must be a float", "roi.right ...",
        #       "roi.top ...", "roi.bottom ..."
        # Do not "fix" this to x1,y1,x2,y2.
        return [x1, x2, y1, y2]

    def _build_json_config(self) -> dict:
        cfg = self._cfg
        alpr = getattr(cfg, "alpr", None)
        assets = (
            (getattr(alpr, "assets_folder", None) if alpr else None)
            or getattr(cfg, "ualpr_assets_folder", None)
            or "/sdk/assets"
        )
        token_file = self._resolve_license_token_file()
        extra: dict = {}
        if token_file:
            # Path only. Deliberately NOT license_token_data — inlining the
            # base64 token would put the licence into config.json.
            extra["license_token_file"] = token_file

        # Everything below comes from config.json alone. The SDK's own
        # `recognizer` sample ships DIFFERENT defaults (pyramidal search on at
        # sensitivity 1.0, rectify on); we must never inherit those. Every key
        # the engine honours is written explicitly here so a sample default
        # cannot leak in.
        adv = getattr(alpr, "engine_advanced", None) if alpr else None
        thr = getattr(alpr, "thresholds", None) if alpr else None
        cls = getattr(alpr, "classifiers", None) if alpr else None

        def _adv(name, default):
            return getattr(adv, name, default) if adv else default

        def _thr(name, default):
            return getattr(thr, name, default) if thr else default

        def _cls(name, legacy_attr, default=False):
            if cls is not None:
                return bool(getattr(cls, name, default))
            return bool(getattr(cfg, legacy_attr, default))

        return {
            **extra,
            "debug_level": "warn",
            "gpgpu_enabled": False,
            "num_threads": int(_adv("num_threads", -1)),
            "assets_folder": assets,
            "charset": "latin",
            "detect_roi": self._detect_roi_pixels(),

            # MUST be explicit. The SDK default is "median", NOT "min" — with
            # the default, confidences[0] would be a median across characters
            # and our whole precision argument collapses: one misread character
            # would be averaged away, which is exactly the failure
            # _to_scalar_confidence() exists to prevent. With "min" set,
            # confidences[0] IS the per-character minimum, so we use it
            # directly rather than recomputing.
            "recogn_score_type": _thr("recogn_score_type", "min"),

            # Hash-stability setting, NOT a tuning knob. Read order is
            # left->right, top->bottom, but it is not yet confirmed whether
            # that ordering is derived from pre- or post-rectification
            # geometry. If post, flipping this could reorder characters on the
            # same physical plate and therefore change its SHA-256. Config
            # warns loudly when enabled.
            "recogn_rectify_enabled": bool(_adv("rectify_enabled", False)),

            # Permissive engine-side floors by default: our app-level
            # Thresholds do the real gating in detector.py, and we want to SEE
            # weak reads rather than have the engine silently drop them.
            "detect_minscore": float(_thr("detect_minscore", 0.1)),
            "recogn_minscore": float(_thr("recogn_minscore", 0.3)),

            # Second-pass search for small/distant plates. The SDK's own
            # recognizer sample turns this ON at sensitivity 1.0; we do not
            # inherit that.
            "pyramidal_search_enabled": bool(
                _adv("pyramidal_search_enabled", False)),
            "pyramidal_search_sensitivity": float(
                _adv("pyramidal_search_sensitivity", 0.28)),

            # --- optional classifiers -------------------------------------
            # Measured on servCamOG (Pi 5, 4 cores, CPU/TF):
            #   plates + car box + VCR ........ 106 ms/frame (~9.4 fps)  CHOSEN
            #   + LPCI + VMMR + VBSR .......... ~268 ms/frame (3.7 fps)  REJECTED
            # The 5 fps budget is 200 ms/frame, so the full stack does not fit.
            "car_noplate_detect_enabled": _cls(
                "vehicle_detect_enabled", "ualpr_car_detect_enabled"),
            "klass_vcr_enabled": _cls("color_enabled", "ualpr_vcr_enabled"),
            "klass_lpci_enabled": _cls("lpci_enabled", "_none"),
            "klass_vmmr_enabled": _cls("vmmr_enabled", "_none"),
            "klass_vbsr_enabled": _cls("vbsr_enabled", "_none"),

            "ienv_enabled": bool(_adv("ienv_enabled", False)),
            "openvino_enabled": bool(_adv("openvino_enabled", False)),
            "npu_enabled": bool(_adv("npu_enabled", False)),
        }

    # -- lifecycle ------------------------------------------------------
    def open(self) -> None:
        global _UALPR_LIVE
        if _UALPR_LIVE is not None and _UALPR_LIVE is not self:
            raise RuntimeError(
                "an UltimateAlprEngine is already open in this process; "
                "ultimateALPR's engine is a process-global singleton, so a "
                "second init() without deInit() is undefined. Call close() on "
                "the first engine before opening another."
            )
        if self._opened:
            return

        _install_imp_shim()
        try:
            import ultimateAlprSdk
        except ImportError as e:
            raise RuntimeError(
                f"ultimateAlprSdk not importable: {e}. There is no macOS "
                "build of this SDK — run inside the container "
                "(see docker/README.md). On a Pi, check that "
                "_ultimateAlprSdk.so is built and that PYTHONPATH includes "
                "both binaries/linux/aarch64 and python/."
            ) from e

        import json
        self._sdk = ultimateAlprSdk
        json_config = self._build_json_config()
        token_file = json_config.get("license_token_file")

        # Log the EFFECTIVE engine config. Without this a run is not
        # reproducible from its report and tuning is guesswork — the Stage 2b
        # run had no record of what was actually set. The token PATH is safe
        # to log; the token contents are never read here.
        _loggable = dict(json_config)
        log.info("ultimateALPR effective engine config: %s",
                 json.dumps(_loggable, sort_keys=True))
        log.info("python %s | frame %sx%s | detect_roi %s",
                 sys.version.split()[0], self._frame_w or "?",
                 self._frame_h or "?", json_config.get("detect_roi"))

        result = self._sdk.UltAlprSdkEngine_init(json.dumps(json_config))
        if not result.isOK():
            raise RuntimeError("UltAlprSdkEngine_init failed")

        # No fast-alpr equivalent: pays the first-inference cost up front so
        # the first real frame isn't an outlier.
        try:
            self._sdk.UltAlprSdkEngine_warmUp(
                self._sdk.ULTALPR_SDK_IMAGE_TYPE_RGB24
            )
        except Exception as e:                       # non-fatal
            log.warning("ultimateALPR warmUp failed (continuing): %s", e)

        self._opened = True
        _UALPR_LIVE = self
        log.info("ultimateALPR engine initialised (licensed=%s)",
                 bool(token_file))

        # Prove the licence actually took effect, before any real frame can
        # produce a hash. See verify_license_unmasked().
        if token_file:
            try:
                if not self.verify_license_unmasked():
                    raise RuntimeError(
                        "licence verification was inconclusive; refusing to "
                        "start a field unit without proving unmasked output"
                    )
            except Exception:
                # open() must be transactional. Without teardown, a failed
                # verification leaves the process-global SDK marked live and
                # prevents a clean retry in this process.
                self.close()
                raise

    def verify_license_unmasked(self) -> bool:
        """Process one known frame and assert the SDK returns UNMASKED text.

        Why this exists, and why it raises rather than warns:

        A token that fails to load does not error. The SDK simply runs in
        trial mode, where it redacts the last character of every plate. That
        redaction then survives NORMALIZATION_SPEC v1.0 — rule 2 strips '*' as
        non-alphanumeric and the 5-6 character remainder clears the rule-3
        floor of 4 — so it hashes cleanly into a confident WRONG vehicle
        identity, with no error anywhere in the stack.

        So "the licence is loaded" cannot be assumed from a successful init().
        It has to be measured, once, at startup, against a plate we can read.
        On a licensed unit this must pass; if it ever fails, the token path is
        broken and the unit must not be allowed to write hashes.

        Returns True on success. Raises RuntimeError on redacted output.
        """
        if self._license_verified:
            return True

        from pathlib import Path
        alpr = getattr(self._cfg, "alpr", None)
        assets = (
            (getattr(alpr, "assets_folder", None) if alpr else None)
            or getattr(self._cfg, "ualpr_assets_folder", None)
            or "/sdk/assets"
        )
        probe = Path(assets) / "images" / "lic_us_1280x720.jpg"
        if not probe.is_file():
            log.warning(
                "licence verification SKIPPED: probe image not found at %s. "
                "The '*' guard in infer() remains active, so a trial-mode "
                "fallback would still be caught on the first real plate.",
                probe,
            )
            return False

        try:
            from PIL import Image
            import numpy as np
            frame = np.array(Image.open(probe).convert("RGB"))
        except ImportError:
            try:
                import cv2
                import numpy as np
                bgr = cv2.imread(str(probe))
                if bgr is None:
                    raise RuntimeError(f"could not decode {probe}")
                frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            except Exception as e:
                log.warning(
                    "licence verification SKIPPED: no image decoder (%s). "
                    "The '*' guard in infer() remains active.", e,
                )
                return False

        import json
        h, w = frame.shape[:2]
        result = self._sdk.UltAlprSdkEngine_process(
            self._sdk.ULTALPR_SDK_IMAGE_TYPE_RGB24,
            frame.tobytes(), w, h, 0, 1,
        )
        if not result.isOK():
            raise EngineContractError(
                "licence verification failed: the SDK could not process the "
                "probe image."
            )

        plates = json.loads(result.json()).get("plates", []) or []
        texts = [p.get("text", "") for p in plates if p.get("text")]
        if not texts:
            log.warning(
                "licence verification INCONCLUSIVE: no plate read from the "
                "probe image. The '*' guard in infer() remains active."
            )
            return False

        redacted = [t for t in texts if "*" in t]
        if redacted:
            raise RuntimeError(
                f"LICENCE NOT ACTIVE: SDK returned redacted text {redacted!r} "
                "from the probe image. The token did not load, so the engine "
                "is in trial mode. Refusing to continue — every plate would "
                "be truncated and hashed into a wrong identity. Check "
                "license_token_file in the unit config."
            )

        self._license_verified = True
        log.info("license verified: unmasked output confirmed (probe read %r)",
                 texts[0])
        return True

    def close(self) -> None:
        global _UALPR_LIVE
        if not self._opened:
            return
        try:
            self._sdk.UltAlprSdkEngine_deInit()
        except Exception as e:
            log.warning("UltAlprSdkEngine_deInit raised: %s", e)
        finally:
            self._opened = False
            self._sdk = None
            if _UALPR_LIVE is self:
                _UALPR_LIVE = None
            log.info("ultimateALPR engine released")

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _quad_to_bbox(warped) -> tuple[int, int, int, int]:
        """Reduce an 8-float quad to an axis-aligned (x1, y1, x2, y2).

        `warpedBox` is [LTx, LTy, RTx, RTy, RBx, RBy, LBx, LBy] — a genuine
        quadrilateral that cannot be represented by two corners. The samples
        observed happened to be axis-aligned, but that is not guaranteed for
        angled plates, so this does the general min/max rather than reading
        indices 0,1,4,5 and hoping.
        """
        if not warped or len(warped) < 8:
            return (0, 0, 0, 0)
        xs = [float(warped[i]) for i in range(0, 8, 2)]
        ys = [float(warped[i]) for i in range(1, 8, 2)]
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

    def _validate_confidences_layout(self, confidences, text) -> None:
        """Verify the binary matches the documented confidences layout.

        Doubango documents confidences as
            [ OCR score, plate detect score, char[0], ..., char[N] ]
        and states that with recogn_score_type="min", element 0 is the minimum
        across the per-character scores.

        We rely on that: `ocr_confidence` is taken straight from element 0
        rather than recomputed. This turns a documentation claim into a
        verified property of the actual .so, once, on the first real read —
        cheap, and it fails before any accuracy work can be built on a wrong
        assumption.
        """
        if self._layout_checked:
            return
        chars = confidences[self._CONF_CHARS_FROM:]
        if not chars:
            return          # nothing to check yet; try again on the next read
        aggregate = float(confidences[self._CONF_OCR])
        expected = min(float(c) for c in chars)
        if abs(aggregate - expected) > 0.5:          # epsilon in 0-100 space
            raise EngineContractError(
                "ultimateALPR confidences layout does not match the "
                f"documented contract for text={text!r}: confidences[0]="
                f"{aggregate:.4f} but min(confidences[2:])={expected:.4f}. "
                "Expected [OCR, detect, char0..charN] with "
                'recogn_score_type="min". Refusing to proceed — '
                "ocr_confidence would be wrong and every threshold with it."
            )
        # len(confidences) should be len(text) + 2; warn rather than raise,
        # since a redacted/odd text shouldn't be fatal on its own.
        if len(confidences) != len(text) + 2:
            log.warning(
                "confidences length %d != len(text)+2 (%d) for %r",
                len(confidences), len(text) + 2, text,
            )
        self._layout_checked = True
        log.info(
            "ultimateALPR confidences layout verified: "
            "confidences[0] == min(per-char)"
        )

    # -- inference ------------------------------------------------------
    def infer(self, frame) -> list[RawPlateRead]:
        if not self._opened or self._sdk is None:
            raise RuntimeError("Detector.open() not called")

        import json
        self.last_call_ok = False
        h, w = frame.shape[:2]
        try:
            result = self._sdk.UltAlprSdkEngine_process(
                self._sdk.ULTALPR_SDK_IMAGE_TYPE_RGB24,
                frame.tobytes(),
                w, h, 0, 1,
            )
        except Exception as e:
            log.warning("ultimateALPR process raised %s; skipping frame", e)
            return []

        if not result.isOK():
            log.warning("ultimateALPR process returned not-OK; skipping frame")
            return []

        try:
            payload = json.loads(result.json())
        except Exception as e:
            log.warning("malformed ultimateALPR result json: %s", e)
            return []

        self.last_call_ok = True

        out: list[RawPlateRead] = []
        for p in payload.get("plates", []) or []:
            text = p.get("text", "") or ""

            # --- the '*' guard. Raise, never log-and-continue. -----------
            # An unlicensed SDK redacts the last character. NORMALIZATION_SPEC
            # rule 2 strips '*' as non-alphanumeric and the 5-6 char remainder
            # clears the rule-3 floor of 4, so a redacted read hashes cleanly
            # into a confident WRONG identity with no error anywhere. Zero
            # legacy hashes does not help: this manufactures bad identities
            # going forward. Measured, not theoretical — see spike §1.3.
            if "*" in text:
                raise RedactedPlateTextError(
                    f"Redacted plate text from unlicensed SDK: {text!r}. "
                    "Refusing to hash — normalize() would strip the '*' and "
                    "produce a confident wrong identity."
                )

            # Vehicle box, when car detection is on. Ships free with base
            # detection and is what makes Problem 2 (vehicle present, plate
            # unreadable) addressable at all.
            car = p.get("car") or {}
            vehicle_bbox = (
                self._quad_to_bbox(car.get("warpedBox")) if car.get("warpedBox")
                else None
            )
            vehicle_conf = (
                float(car["confidence"]) / 100.0
                if car.get("confidence") is not None else None
            )

            confidences = p.get("confidences", []) or []
            if not text or len(confidences) < self._CONF_CHARS_FROM:
                # Localized but unreadable. Still a real detection for
                # presence purposes; detector.py will mark it below_threshold.
                out.append(RawPlateRead(
                    text="",
                    detection_confidence=0.0,
                    ocr_confidence=0.0,
                    bbox=self._quad_to_bbox(p.get("warpedBox")),
                    vehicle_bbox=vehicle_bbox,
                    vehicle_confidence=vehicle_conf,
                ))
                continue

            self._validate_confidences_layout(confidences, text)

            # Scale: SDK returns 0-100, RawPlateRead is contractually 0.0-1.0.
            # confidences[0] is the aggregate OCR score, which IS the
            # per-character minimum because recogn_score_type is "min".
            # Do NOT recompute min() over the raw array — index 1 is the plate
            # DETECTION score, not a character score.
            ocr_conf = float(confidences[self._CONF_OCR]) / 100.0
            det_conf = float(confidences[self._CONF_DETECT]) / 100.0

            out.append(RawPlateRead(
                text=text,
                detection_confidence=det_conf,
                ocr_confidence=ocr_conf,
                bbox=self._quad_to_bbox(p.get("warpedBox")),
                vehicle_bbox=vehicle_bbox,
                vehicle_confidence=vehicle_conf,
            ))
        return out


def _stream_webhook_engine(cfg):
    """Imported lazily: pulls in http.server/email only when actually used."""
    from .stream_engine import StreamWebhookEngine
    return StreamWebhookEngine(cfg)


_ENGINES = {
    FastAlprEngine.name: FastAlprEngine,
    UltimateAlprEngine.name: UltimateAlprEngine,
    # Plate Recognizer Stream. Recognition happens in the Stream container;
    # this engine is the webhook receiver. See stream_engine.py.
    "pr_stream": _stream_webhook_engine,
}


def build_engine(cfg) -> PlateEngine:
    """Pick an engine from config.

    Defaults to fast-alpr when the key is absent, so an existing config.json
    (and the stub configs used by the tests) behave exactly as before.
    """
    name = getattr(cfg, "engine", FastAlprEngine.name) or FastAlprEngine.name
    try:
        factory = _ENGINES[name]
    except KeyError:
        raise ValueError(
            f"unknown engine {name!r}; expected one of {sorted(_ENGINES)}"
        ) from None
    return factory(cfg)
