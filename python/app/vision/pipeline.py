"""The scanning pipeline: capture thread + decode thread + statistics.

Threading model
---------------
::

    capture thread  --publishes-->  FrameSlot  --takes-->  decode thread
                                                              |
                                            verify (ScanService) --> UI

Why two threads rather than one: a V4L2 read blocks, and a decode takes tens of
milliseconds.  Sharing one thread means the device buffer overflows during a
slow decode and the driver starts dropping frames.  Splitting them keeps
capture at camera rate and lets the decode loop skip stale frames
(``max_frame_age_ms``) instead of queueing work it can never finish.

Frame source ownership (ADR-006) is enforced by :meth:`ScannerPipeline.start`:
only the process whose ``stream_source`` matches the running mode may own the
camera, and a second attempt raises ``CAMERA_IN_USE``.

Workflow states
---------------
Every state change goes through :class:`~app.services.scan_workflow_service.
ScanWorkflowService`, never through a local attribute.  The capture half of the
workflow (IDLE -> CAMERA_INITIALIZING -> SCANNING -> BARCODE_DETECTED ->
DECODING -> VALIDATING -> LOOKING_UP_STUDENT) lives here; the transaction half
is driven by the API.  Both halves share one machine, so
``GET /api/scanner/state`` can report a single, coherent state.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from app.services.scan_service import ScanService, get_scan_service
from app.services.scan_workflow import ScanState
from app.services.scan_workflow_service import ScanWorkflowService, get_workflow
from app.utils.config import AppConfig, get_config
from app.utils.errors import ErrorCode
from app.utils.logger import get_logger
from app.utils.timeutil import utc_now_iso
from app.vision.camera import Camera
from app.vision.decoder import BarcodeDecoder, Detection, get_decoder
from app.vision.preprocess import ImagePreprocessor, annotate

logger = get_logger(__name__)

#: Every canonical state name, for clients that validate the ``state`` field.
#: The authoritative table lives in :mod:`app.services.scan_workflow`.
STATES = tuple(str(state) for state in ScanState)


@dataclass(slots=True)
class FrameStats:
    """Rolling performance counters exposed by ``GET /api/scanner/state``."""

    frames_captured: int = 0
    frames_processed: int = 0
    frames_skipped: int = 0
    decode_failures: int = 0
    scans_accepted: int = 0
    scans_rejected: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_frame_at: float = 0.0
    fps: float = 0.0
    _fps_samples: deque = field(default_factory=lambda: deque(maxlen=30), repr=False)
    processing_ms: deque = field(default_factory=lambda: deque(maxlen=120), repr=False)
    detection_ms: deque = field(default_factory=lambda: deque(maxlen=120), repr=False)
    database_ms: deque = field(default_factory=lambda: deque(maxlen=120), repr=False)

    def record_frame(self) -> None:
        now = time.monotonic()
        self.frames_captured += 1
        if self.last_frame_at:
            interval = now - self.last_frame_at
            if interval > 0:
                self._fps_samples.append(1.0 / interval)
        self.last_frame_at = now
        self.fps = round(float(np.mean(self._fps_samples)), 2) if self._fps_samples else 0.0

    def record_processing(self, processing_ms: float, detection_ms: float | None,
                          database_ms: float | None) -> None:
        self.frames_processed += 1
        self.processing_ms.append(processing_ms)
        if detection_ms is not None:
            self.detection_ms.append(detection_ms)
        if database_ms is not None:
            self.database_ms.append(database_ms)

    def record_database(self, database_ms: float | None) -> None:
        if database_ms is not None:
            self.database_ms.append(database_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames_captured": self.frames_captured,
            "frames_processed": self.frames_processed,
            "frames_skipped": self.frames_skipped,
            "decode_failures": self.decode_failures,
            "scans_accepted": self.scans_accepted,
            "scans_rejected": self.scans_rejected,
            "fps": self.fps,
            "avg_processing_ms": _avg(self.processing_ms),
            "avg_detection_ms": _avg(self.detection_ms),
            "avg_database_ms": _avg(self.database_ms),
            "uptime_s": round(time.monotonic() - self.started_at, 1),
        }


def _avg(values: deque) -> float | None:
    if not values:
        return None
    return round(float(np.mean(values)), 3)


class ScannerPipeline:
    """Owns the camera, the decoder and the scan callback."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        scan_service: ScanService | None = None,
        decoder: BarcodeDecoder | None = None,
        on_scan: Callable[[Any], None] | None = None,
        owner: str | None = None,
        workflow: ScanWorkflowService | None = None,
    ) -> None:
        self.config = config or get_config()
        self.camera = Camera(self.config)
        self.preprocess = ImagePreprocessor(self.config.preprocessing, self.config.camera)
        self.decoder = decoder or get_decoder()
        self.scans = scan_service or get_scan_service()
        self.on_scan = on_scan
        self.owner = owner or "python"
        self.workflow = workflow or get_workflow()
        self.stats = FrameStats()
        self.last_detection: Detection | None = None
        self.last_result: str | None = None
        self.last_barcode: str | None = None
        self._detections: list[Detection] = []
        self._display_frame: np.ndarray | None = None
        self._display_lock = threading.Lock()
        self._capture_thread: threading.Thread | None = None
        self._decode_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status_text = ""
        self._status_colour = (240, 240, 240)
        self._updated_at = utc_now_iso()

    # -- workflow ----------------------------------------------------------
    @property
    def state(self) -> str:
        """The current canonical workflow state."""
        return str(self.workflow.state)

    @property
    def error_code(self) -> str | None:
        return self.workflow.error_code

    @property
    def error_message(self) -> str | None:
        return self.workflow.error_message

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, background: bool = True) -> bool:
        if self._capture_thread and self._capture_thread.is_alive():
            logger.debug("pipeline already running")
            return True
        self._stop.clear()
        self.workflow.begin_scan()
        if not self.camera.open():
            self.workflow.camera_error(
                self.camera.error or ErrorCode.CAMERA_NOT_FOUND,
                (self.camera.error or ErrorCode.CAMERA_NOT_FOUND).user_message,
            )
            self._touch()
            logger.error("pipeline could not start: %s", self.workflow.error_code)
            return False

        self.workflow.camera_ready()
        self._touch()

        if background:
            self._capture_thread = threading.Thread(
                target=self._capture_loop, name="camera-capture", daemon=True
            )
            self._decode_thread = threading.Thread(
                target=self._decode_loop, name="barcode-decode", daemon=True
            )
            self._capture_thread.start()
            self._decode_thread.start()
            logger.info("scanner pipeline started (owner=%s)", self.owner)
        return True

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._decode_thread, self._capture_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._capture_thread = None
        self._decode_thread = None
        self.camera.close()
        self.workflow.stop()
        self._touch()
        logger.info("scanner pipeline stopped")

    @property
    def running(self) -> bool:
        return bool(self._capture_thread and self._capture_thread.is_alive())

    # -- threads -----------------------------------------------------------
    def _capture_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            frame, error = self.camera.read()
            if frame is None:
                failures += 1
                # A disconnect stops the scan loop; the operator has to press
                # Retry Camera (or use manual entry) to resume.
                self.workflow.camera_error(
                    error or ErrorCode.FRAME_CAPTURE_FAILED,
                    (error or ErrorCode.FRAME_CAPTURE_FAILED).user_message,
                )
                self._touch()
                if failures >= 5:
                    # Reconnect with a short backoff instead of spinning.
                    logger.warning("reopening camera after %d failed reads", failures)
                    failures = 0
                    self._stop.wait(1.0)
                    if self.camera.open():
                        self.workflow.camera_ready("camera reopened")
                else:
                    self._stop.wait(0.05)
                continue
            failures = 0
            self.stats.record_frame()
            if self.workflow.state in {ScanState.CAMERA_ERROR, ScanState.CAMERA_INITIALIZING}:
                # The retry succeeded: the next frame resumes the workflow.
                self.workflow.camera_ready("capture resumed")
                self._touch()

    def _decode_loop(self) -> None:
        last_sequence = -1
        while not self._stop.is_set():
            frame, sequence, age_ms = self.camera.slot.take(last_sequence)
            if frame is None:
                self._stop.wait(0.005)
                continue
            last_sequence = sequence
            if age_ms > 500.0:
                self.stats.frames_skipped += 1
                continue
            try:
                self._process_frame(frame)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the scanner
                logger.exception("frame processing failed: %s", exc)
                self.workflow.database_error(
                    ErrorCode.UNKNOWN_ERROR,
                    "An unexpected error occurred while processing a frame.",
                )
                self._touch()
                time.sleep(0.05)

    # -- per-frame work ----------------------------------------------------
    def _process_frame(self, frame: np.ndarray) -> None:
        started = time.perf_counter()
        scan_cfg = self.config.scanner
        # Rule 9.5.1: a card held at the counter is decoded on every frame.
        # While the workflow cannot accept a new symbol - a lookup or a
        # transaction is in flight, or the camera is down - there is no point
        # decoding, and certainly no point querying the database.
        if not self.workflow.can_accept_scan():
            self.stats.frames_skipped += 1
            return

        pre = self.preprocess.process(frame)
        # Nothing is written to the log for a frame with no symbol in it: the
        # workflow stays in SCANNING and process_scan is never reached
        # (rule 9.5.1).
        decode = self.decoder.decode(pre.image)
        attempts = 1

        if not decode.found and scan_cfg.decode_attempts > 1:
            # Second attempt on the original frame: aggressive thresholding that
            # would hurt a good frame is worth trying on a hard one.
            self.preprocess.force_aggressive = True
            try:
                alt = self.preprocess.process(frame, grayscale_only=False)
                alt_decode = self.decoder.decode(alt.image)
                attempts += 1
                if alt_decode.found:
                    decode = alt_decode
            finally:
                self.preprocess.force_aggressive = False

        processing_ms = (time.perf_counter() - started) * 1000.0
        self.stats.record_processing(
            processing_ms, decode.duration_ms, None
        )
        if not decode.found:
            self.stats.decode_failures += 1
        logger.debug("frame decoded in %.2f ms (%d attempt(s), found=%s)",
                     processing_ms, attempts, decode.found)

        self._detections = decode.detections
        self.last_detection = decode.best
        self._update_display(frame, decode.found)

        detection = decode.best
        if detection is None or not detection.valid:
            # Nothing decodable in this frame.  A symbol that was located but
            # could not be read is a real BARCODE_INVALID, not a "no barcode"
            # frame, so it is reported as such.
            if decode.error_code and str(decode.error_code) == str(
                ErrorCode.BARCODE_DECODE_FAILED
            ):
                self.workflow.barcode_detected(reason="a symbol was found but unreadable")
                self.workflow.barcode_invalid(
                    decode.error_code, reason="the symbol could not be decoded"
                )
            else:
                self.workflow.resume_scanning("no barcode in view")
            self._touch()
            return

        # A decoded symbol.  ScanService owns the cooldown so the browser, the
        # C++ scanner and the API all share one duplicate policy; the workflow
        # service owns the state walk (DECODING -> VALIDATING ->
        # LOOKING_UP_STUDENT -> a terminal state).
        self.last_barcode = detection.text
        response = self.workflow.scan_payload(
            detection.text,
            scans=self.scans,
            barcode_type=detection.format,
            confidence=None,  # ZXing-C++ exposes no numeric score (ADR-014)
            source="python",
            device_name=self.camera.source_label or self.owner,
            processing_time_ms=round(processing_ms, 3),
            detection_time_ms=round(decode.duration_ms, 3),
        )
        if response is None:
            # The workflow could not take the payload (a transaction started
            # between the decode and the lookup). Nothing was recorded.
            self._touch()
            return
        self.last_result = str(response.result)
        if str(response.result) == "VERIFIED":
            self.stats.scans_accepted += 1
        else:
            self.stats.scans_rejected += 1
        self.stats.record_database(response.timing.database_ms)
        self._update_status(response.message, response.success)
        self._touch()
        if self.on_scan is not None:
            try:
                self.on_scan(response)
            except Exception:  # noqa: BLE001 - a UI callback cannot break capture
                logger.exception("scan callback raised")

    def _update_display(self, frame: np.ndarray, found: bool) -> None:
        detections = [
            {
                "points": d.points,
                "text": d.text if d.valid else f"{d.text} ({d.error_type or 'invalid'})",
                "colour": (80, 220, 120) if d.valid else (60, 60, 235),
            }
            for d in self._detections
        ]
        annotated = annotate(
            frame,
            self.config.camera,
            detections,
            status_text=self._status_text or ("SCANNING" if not found else ""),
            status_colour=self._status_colour,
        )
        with self._display_lock:
            self._display_frame = annotated

    def _update_status(self, message: str, success: bool) -> None:
        self._status_text = message[:80]
        self._status_colour = (120, 230, 140) if success else (255, 200, 90)

    def _touch(self) -> None:
        self._updated_at = utc_now_iso()

    # -- consumers ---------------------------------------------------------
    def display_frame(self) -> np.ndarray | None:
        with self._display_lock:
            return self._display_frame

    def snapshot(self) -> dict[str, Any]:
        data = self.stats.as_dict()
        workflow = self.workflow.snapshot()
        data.update(
            {
                # `state` stays at the top level for backwards compatibility
                # with older clients; `workflow` carries the full machine.
                "state": workflow["state"],
                "stream_source": self.config.scanner.stream_source,
                "owner": self.owner,
                "camera_connected": self.camera.is_open,
                "camera_device": self.camera.source_label or None,
                "last_detection_ms": data.get("avg_detection_ms"),
                "last_database_ms": data.get("avg_database_ms"),
                "last_barcode": self.last_barcode,
                "last_result": self.last_result,
                "cooldowns_active": self.scans.cooldowns.active_count(),
                "error_code": workflow["error_code"],
                "error_message": workflow["error_message"],
                "updated_at": self._updated_at,
                "detections": [d.as_dict() for d in self._detections],
                "workflow": workflow,
            }
        )
        return data


#: The process-wide pipeline used by the MJPEG route and the API.
_pipeline: ScannerPipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> ScannerPipeline:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = ScannerPipeline(owner="python")
        return _pipeline


def set_pipeline(pipeline: ScannerPipeline | None) -> None:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None and pipeline is not None and _pipeline is not pipeline:
            _pipeline.stop()
        _pipeline = pipeline
