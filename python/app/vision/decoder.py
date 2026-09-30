"""Barcode detection and decoding (ZXing-C++ via ``zxing-cpp``).

Real decoding only.  This module never fabricates a payload: when nothing is
found it returns an empty result, and the scanner reports
``BARCODE_NOT_DETECTED``.

Confidence
----------
ZXing-C++ validates every symbol it returns (checksum for EAN/UPC/Code 128,
error correction for QR/DataMatrix) but does **not** expose a numeric score, so
``confidence`` is reported as ``None`` rather than invented.  ``valid=False``
results carry ``error_type`` and are surfaced as ``BARCODE_DECODE_FAILED``.
See ADR-014.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.utils.config import AppConfig, get_config
from app.utils.errors import ErrorCode
from app.utils.logger import get_logger
from app.services.verification_service import normalize_format

logger = get_logger(__name__)

#: Symbologies the pipeline is willing to *attempt*.  A superset of the
#: verification allow-list: we may detect a DataMatrix in order to answer
#: "unsupported format" instead of silently finding nothing.
_ATTEMPT_FORMATS = (
    "Code128",
    "Code39",
    "Code93",
    "Codabar",
    "ITF",
    "EAN13",
    "EAN8",
    "UPCA",
    "UPCE",
    "QRCode",
    "DataMatrix",
    "Aztec",
    "PDF417",
    "DataBar",
)


@dataclass(slots=True)
class Detection:
    """One decoded symbol found in a frame."""

    text: str
    format: str | None = None
    symbology: str | None = None
    valid: bool = True
    error_type: str | None = None
    orientation: float = 0.0
    points: list[list[int]] = field(default_factory=list)
    content_type: str | None = None
    symbology_identifier: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "format": self.format,
            "symbology": self.symbology,
            "valid": self.valid,
            "error_type": self.error_type,
            "orientation": self.orientation,
            "points": self.points,
        }


@dataclass(slots=True)
class DecodeResult:
    detections: list[Detection] = field(default_factory=list)
    error_code: str | None = None
    attempts: int = 0
    duration_ms: float = 0.0
    used_preprocessed: bool = False

    @property
    def found(self) -> bool:
        return bool(self.detections)

    @property
    def best(self) -> Detection | None:
        return self.detections[0] if self.detections else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "detections": [d.as_dict() for d in self.detections],
            "error_code": self.error_code,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 3),
            "used_preprocessed": self.used_preprocessed,
        }


class BarcodeDecoder:
    """Thin, testable wrapper around ``zxingcpp.read_barcodes``."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._formats = self._build_formats()

    def _build_formats(self):
        import zxingcpp

        names = ", ".join(_ATTEMPT_FORMATS)
        try:
            return zxingcpp.barcode_formats_from_str(names)
        except Exception:  # noqa: BLE001 - fall back to "all formats"
            logger.warning("could not build the symbology mask; decoding all formats")
            return None

    def decode(
        self,
        image: np.ndarray,
        *,
        try_rotate: bool = True,
        try_invert: bool = True,
        try_downscale: bool = True,
    ) -> DecodeResult:
        import time

        import zxingcpp

        started = time.perf_counter()
        if image is None or image.size == 0:
            return DecodeResult(error_code=str(ErrorCode.FRAME_CAPTURE_FAILED))

        try:
            results = zxingcpp.read_barcodes(
                image,
                formats=self._formats,
                try_rotate=try_rotate,
                try_invert=try_invert,
                try_downscale=try_downscale,
            )
        except TypeError:  # pragma: no cover - older/newer keyword set
            results = zxingcpp.read_barcodes(image, formats=self._formats)
        except Exception as exc:  # noqa: BLE001 - a decoder crash must not kill the loop
            logger.error("barcode decoder raised: %s", exc)
            return DecodeResult(error_code=str(ErrorCode.BARCODE_DECODE_FAILED),
                                duration_ms=(time.perf_counter() - started) * 1000.0)

        detections: list[Detection] = []
        for item in results or []:
            text = (item.text or "").strip()
            if not text:
                continue
            detections.append(
                Detection(
                    text=text,
                    format=normalize_format(str(item.format)) or str(item.format),
                    symbology=str(getattr(item, "symbology", "") or "") or None,
                    valid=bool(getattr(item, "valid", True)),
                    error_type=(str(item.error.name) if getattr(item, "error", None) else None),
                    orientation=float(getattr(item, "orientation", 0) or 0),
                    points=_position_points(item),
                    content_type=str(getattr(item, "content_type", "") or "") or None,
                    symbology_identifier=str(
                        getattr(item, "symbology_identifier", "") or ""
                    ) or None,
                )
            )

        error_code: str | None = None
        if not detections:
            error_code = str(ErrorCode.BARCODE_NOT_DETECTED)
        elif not any(d.valid for d in detections):
            error_code = str(ErrorCode.BARCODE_DECODE_FAILED)

        return DecodeResult(
            detections=detections,
            error_code=error_code,
            attempts=1,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    def decode_image_file(self, path: str) -> DecodeResult:
        """Decode a still image (used by the CLI, the tests and the fixtures)."""
        import cv2

        image = cv2.imread(path, cv2.IMREAD_COLOR)
        if image is None:
            logger.error("could not read image %s", path)
            return DecodeResult(error_code=str(ErrorCode.FILE_ERROR))
        return self.decode(image)


def _position_points(item: Any) -> list[list[int]]:
    position = getattr(item, "position", None)
    if position is None:
        return []
    points: list[list[int]] = []
    for name in ("top_left", "top_right", "bottom_right", "bottom_left"):
        point = getattr(position, name, None)
        if point is not None:
            points.append([int(point.x), int(point.y)])
    return points


_decoder: BarcodeDecoder | None = None


def get_decoder() -> BarcodeDecoder:
    global _decoder
    if _decoder is None:
        _decoder = BarcodeDecoder()
    return _decoder
