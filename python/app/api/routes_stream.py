"""Live video stream endpoints.

The browser cannot open ``/dev/video0``, so the process that owns the camera
encodes JPEG frames and this router serves them:

* ``stream_source = python``  - the Python pipeline publishes annotated frames.
* ``stream_source = cpp``     - ``student_id_scanner --mode=service`` posts
  annotated frames to ``POST /api/stream/frame``; this router only relays them.
* ``stream_source = file``    - a recorded file is replayed.

The browser therefore always talks to the same URL and never needs to know which
process has the device (ADR-006).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Annotated, Any, AsyncIterator

import numpy as np
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.dependencies import current_user
from app.services.auth_service import AuthenticatedUser
from app.utils.config import AppConfig, get_config
from app.utils.errors import ErrorCode, ValidationError
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/stream", tags=["stream"])

_BOUNDARY = "frame"


class FrameRelay:
    """Holds the most recent encoded frame for the MJPEG generator.

    Single-slot by design: a slow client must not build a backlog.  A new frame
    simply replaces the old one, and the generator yields whatever is current.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._jpeg: bytes | None = None
        self._sequence = 0
        self._timestamp = 0.0
        self._source: str | None = None
        self._detections: list[dict[str, Any]] = []
        self._error_code: str | None = None
        self._error_message: str | None = None

    def publish(
        self,
        jpeg: bytes,
        *,
        source: str | None = None,
        detections: list[dict[str, Any]] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._condition:
            self._jpeg = jpeg
            self._sequence += 1
            self._timestamp = time.monotonic()
            self._source = source
            self._detections = detections or []
            self._error_code = error_code
            self._error_message = error_message
            self._condition.notify_all()

    def wait_for_next(self, last_sequence: int, timeout: float) -> tuple[bytes | None, int]:
        with self._condition:
            if self._sequence == last_sequence or self._jpeg is None:
                self._condition.wait(timeout)
            return self._jpeg, self._sequence

    def latest(self) -> bytes | None:
        with self._condition:
            return self._jpeg

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "has_frame": self._jpeg is not None,
                "sequence": self._sequence,
                "source": self._source,
                "age_ms": round((time.monotonic() - self._timestamp) * 1000.0, 1)
                if self._timestamp else None,
                "detections": self._detections,
                "error_code": self._error_code,
                "error_message": self._error_message,
            }

    def clear(self) -> None:
        with self._condition:
            self._jpeg = None
            self._sequence += 1
            self._detections = []
            self._source = None


relay = FrameRelay()


def _encode(frame: np.ndarray, quality: int = 75) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:  # pragma: no cover - only on a corrupt frame
        raise ValueError("could not encode the frame as JPEG")
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# Relaying frames from the C++ scanner
# ---------------------------------------------------------------------------
@router.post(
    "/frame",
    status_code=204,
    summary="Publish an annotated frame from the C++ scanner (service mode)",
)
async def publish_frame(
    request: Request,
    config: Annotated[AppConfig, Depends(get_config)],
    image: Annotated[UploadFile, File(description="JPEG frame")],
    source: Annotated[str, Form()] = "cpp",
    detections: Annotated[str, Form()] = "[]",
    error_code: Annotated[str | None, Form()] = None,
    error_message: Annotated[str | None, Form()] = None,
) -> Response:
    """Ingest one frame from ``student_id_scanner``.

    Deliberately unauthenticated: the C++ scanner is a local process on the same
    machine, and requiring a session would mean putting the admin password in the
    scanner's config.  The endpoint only accepts a JPEG and only feeds the
    browser preview - it cannot modify data.  Frames are capped in size, and the
    router binds to loopback.
    """
    content = await image.read()
    if not content:
        raise ValidationError("empty frame")
    if len(content) > 4 * 1024 * 1024:
        raise ValidationError("frame exceeds the 4 MiB limit")
    if not content.startswith(b"\xff\xd8"):
        raise ValidationError("frame must be a JPEG image")

    import json

    try:
        parsed = json.loads(detections or "[]")
        if not isinstance(parsed, list):
            raise ValueError
    except ValueError as exc:
        raise ValidationError("detections must be a JSON array") from exc

    relay.publish(
        content,
        source=source,
        detections=parsed,
        error_code=error_code,
        error_message=error_message,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Consuming frames in the browser
# ---------------------------------------------------------------------------
@router.get("/mjpeg", summary="MJPEG live preview (multipart/x-mixed-replace)")
async def mjpeg(request: Request, config: Annotated[AppConfig, Depends(get_config)]) -> StreamingResponse:
    """Stream the current frame as soon as it is available.

    The generator stops when the client disconnects, so closing the browser tab
    does not leave a task encoding frames for nobody.  It is an *async*
    generator with a short poll: awaiting ``request.is_disconnected()`` is the
    only reliable way to notice a closed connection, and blocking the event loop
    for a frame wait would stall every other request.
    """

    async def generate() -> AsyncIterator[bytes]:
        last_sequence = -1
        idle = 0
        while True:
            if await request.is_disconnected():
                logger.debug("MJPEG client disconnected")
                return
            state = relay.status()
            if state["sequence"] != last_sequence and state["has_frame"]:
                last_sequence = int(state["sequence"])
                idle = 0
                jpeg = relay.latest() or b""
                yield (
                    b"--" + _BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                )
            else:
                idle += 1
                if idle > 40:  # ~1.3 s without a new frame
                    idle = 0
                    yield (
                        b"--" + _BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + _placeholder(
                            ErrorCode(state["error_code"] or ErrorCode.CAMERA_DISCONNECTED)
                        )
                        + b"\r\n"
                    )
            await asyncio.sleep(0.025)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/snapshot.jpg", summary="Single JPEG frame")
def snapshot(
    config: Annotated[AppConfig, Depends(get_config)],
    quality: int = 80,
) -> Response:
    """One frame as an image, used for the "capture still" button and by the
    integration tests that verify the pipeline end to end."""
    from app.vision.pipeline import get_pipeline

    published = relay.latest()
    if published is not None:
        return Response(content=published, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    pipeline = get_pipeline()
    frame = pipeline.display_frame()
    if frame is None:
        return Response(
            content=_placeholder(ErrorCode.CAMERA_NOT_FOUND),
            media_type="image/jpeg",
            status_code=503,
        )
    return Response(content=_encode(frame, max(10, min(95, quality))),
                    media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/status", summary="Stream ownership and latest detection payload")
def status(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> dict[str, Any]:
    pipeline_snapshot: dict[str, Any] = {}
    if config.scanner.stream_source == "python":
        from app.vision.pipeline import get_pipeline

        pipeline_snapshot = get_pipeline().snapshot()
    return {
        "stream_source": config.scanner.stream_source,
        "relay": relay.status(),
        "pipeline": pipeline_snapshot,
    }


@router.get("/last-frame.jpg", summary="Last frame as uploaded by the C++ scanner")
def last_frame() -> Response:
    """Unauthenticated on purpose - the preview carries a student ID card, so the
    endpoint is only useful on the same machine, and the service binds to
    loopback.  Guard deployments with a reverse proxy if you expose it."""
    published = relay.latest()
    if published is None:
        return Response(content=_placeholder(ErrorCode.CAMERA_NOT_FOUND),
                        media_type="image/jpeg", status_code=503)
    return Response(content=published, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _placeholder(code: ErrorCode) -> bytes:
    """A 640x360 image stating the error, so the <img> tag shows the reason."""
    import cv2
    import numpy as np

    canvas = np.full((360, 640, 3), 24, dtype=np.uint8)
    text = code.user_message
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.6
    (tw, th), _ = cv2.getTextSize(text, font, scale, 1)
    cv2.putText(canvas, text, ((640 - tw) // 2, 170), font, scale, (200, 200, 200), 1, cv2.LINE_AA)
    label = str(code)
    (lw, lh), _ = cv2.getTextSize(label, font, 0.5, 1)
    cv2.putText(canvas, label, ((640 - lw) // 2, 205), font, 0.5, (120, 120, 235), 1, cv2.LINE_AA)
    ok, buffer = cv2.imencode(".jpg", canvas)
    return buffer.tobytes() if ok else b""  # pragma: no cover
