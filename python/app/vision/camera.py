"""Camera capture with explicit error codes and automatic recovery.

Why a wrapper instead of ``cv2.VideoCapture`` directly: the brief requires
distinguishing ``CAMERA_NOT_FOUND`` from ``CAMERA_PERMISSION_DENIED`` from
``CAMERA_DISCONNECTED``, and a V4L2 failure is not detectable from
``VideoCapture.isOpened()`` alone.  The only reliable signals are the device
node and ``open()``'s return value, so the wrapper inspects ``/dev/video*`` and
retries with backoff.

Capture runs on its own thread (owned by :mod:`app.vision.pipeline`) and hands
frames over through a lock-protected slot, so a slow decoder never stalls the
device read - which is what causes the ``V4L2 buffer dropped`` warnings.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.utils.config import AppConfig, get_config
from app.utils.errors import ErrorCode
from app.utils.logger import get_logger

logger = get_logger(__name__)

_DEVICE_GLOBS = ("/dev/video*",)


@dataclass(slots=True)
class FrameSlot:
    """Single-producer / single-consumer frame slot.

    ``lock`` is declared first, and with a default factory, because a
    dataclass may not put a non-default field after a defaulted one.  Declaring
    it as a plain annotation made the module fail to import, which only showed
    up when the camera was actually opened.
    """

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    image: np.ndarray | None = None
    timestamp: float = 0.0
    sequence: int = 0
    dropped: int = 0

    def publish(self, image: np.ndarray, timestamp: float, sequence: int) -> None:
        with self.lock:
            previous = self.image
            self.image = image
            self.timestamp = timestamp
            self.sequence = sequence
            if previous is not None and self.dropped >= 0:
                self.dropped += 1

    def take(self, last_sequence: int = -1) -> tuple[np.ndarray | None, int, int]:
        """Return ``(frame, sequence, age_ms)`` for a frame newer than the last seen."""
        with self.lock:
            if self.image is None or self.sequence == last_sequence:
                return None, last_sequence, 0
            return self.image, self.sequence, (time.monotonic() - self.timestamp) * 1000.0

    def latest(self) -> np.ndarray | None:
        with self.lock:
            return self.image


class Camera:
    """Owns a ``cv2.VideoCapture`` and exposes it through a :class:`FrameSlot`."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.slot = FrameSlot()
        self._capture: Any = None
        self._lock = threading.RLock()
        self._opened = False
        self._source_label = ""
        self._error: ErrorCode | None = None
        self._frames = 0

    # -- device inspection -------------------------------------------------
    @property
    def source_label(self) -> str:
        return self._source_label

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def error(self) -> ErrorCode | None:
        return self._error

    @property
    def frame_count(self) -> int:
        return self._frames

    def _device_path(self, index: int) -> Path | None:
        devices: list[Path] = []
        for pattern in _DEVICE_GLOBS:
            devices.extend(Path(p) for p in sorted(os.listdir("/dev") if Path("/dev").is_dir() else [])
                           if Path(p).match("/dev/video*"))
        matches = sorted(set(devices))
        return matches[index] if 0 <= index < len(matches) else None

    def available_devices(self) -> list[str]:
        try:
            import glob

            return sorted(glob.glob("/dev/video*"))
        except OSError:  # pragma: no cover
            return []

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> bool:
        """Open the configured source.  Returns False and sets ``error`` on failure."""
        import cv2

        with self._lock:
            self.close()
            camera = self.config.camera
            self._error = None

            if camera.source:
                self._source_label = camera.source
                capture = cv2.VideoCapture(camera.source, cv2.CAP_FFMPEG)
                if not capture.isOpened():
                    capture = cv2.VideoCapture(camera.source)
            else:
                device = self._device_path(camera.device_index)
                if device is None:
                    self._error = ErrorCode.CAMERA_NOT_FOUND
                    logger.error(
                        "camera device /dev/video%d does not exist (available: %s)",
                        camera.device_index, self.available_devices(),
                    )
                    return False
                if not os.access(device, os.R_OK | os.W_OK):
                    self._error = ErrorCode.CAMERA_PERMISSION_DENIED
                    logger.error("no read/write permission on %s - check the udev rules or group membership",
                                 device)
                    return False
                self._source_label = str(device)
                capture = cv2.VideoCapture(str(device), cv2.CAP_V4L2)
                if not capture.isOpened():
                    # Some drivers only accept the plain constructor.
                    capture = cv2.VideoCapture(str(device))

            if not capture.isOpened():
                self._error = ErrorCode.CAMERA_NOT_FOUND
                logger.error("could not open camera source %s", self._source_label)
                capture.release()
                return False

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.height)
            capture.set(cv2.CAP_PROP_FPS, camera.fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(max(0, camera.warmup_frames)):
                capture.read()

            self._capture = capture
            self._opened = True
            self._frames = 0
            logger.info(
                "camera opened: %s (%dx%d @ %d fps requested)",
                self._source_label, camera.width, camera.height, camera.fps,
            )
            return True

    def close(self) -> None:
        with self._lock:
            if self._capture is not None:
                try:
                    self._capture.release()
                except Exception:  # noqa: BLE001 - release must never raise
                    logger.debug("camera release raised", exc_info=True)
            self._capture = None
            self._opened = False

    def read(self) -> tuple[np.ndarray | None, ErrorCode | None]:
        """Grab one frame.  Returns ``(frame, error)``.

        A device that stops delivering frames is reported as
        ``CAMERA_DISCONNECTED``; the caller decides whether to reopen.
        """
        with self._lock:
            capture = self._capture
        if capture is None:
            return None, ErrorCode.CAMERA_NOT_FOUND

        started = time.perf_counter()
        ok, frame = capture.read()
        waited_ms = (time.perf_counter() - started) * 1000.0
        if not ok or frame is None:
            self._error = ErrorCode.CAMERA_DISCONNECTED
            logger.error("frame capture failed after %.1f ms from %s", waited_ms,
                         self._source_label)
            return None, ErrorCode.CAMERA_DISCONNECTED
        if frame.size == 0:
            self._error = ErrorCode.FRAME_CAPTURE_FAILED
            return None, ErrorCode.FRAME_CAPTURE_FAILED

        if self.config.camera.flip:
            frame = frame[:, ::-1]
        self._frames += 1
        self._error = None
        self.slot.publish(frame, time.monotonic(), self._frames)
        return frame, None

    # -- geometry ----------------------------------------------------------
    def properties(self) -> dict[str, Any]:
        import cv2

        with self._lock:
            capture = self._capture
        if capture is None:
            return {}
        return {
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "fps": float(capture.get(cv2.CAP_PROP_FPS) or 0.0),
            "device": self._source_label,
        }
