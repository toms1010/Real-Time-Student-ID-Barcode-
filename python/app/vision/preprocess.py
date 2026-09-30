"""Adaptive image preprocessing.

This mirrors ``cpp/src/proprocessing/ImagePreprocessor.cpp`` step for step so the
two implementations behave identically, and it follows ADR-007: **no step is
applied unconditionally.**  Each one is gated on a measured property of the
frame, because blind filtering measurably hurts Code 128 reads:

============================  ==========================================
step                          applied when
============================  ==========================================
ROI crop                      ``enable_roi_crop`` and the ROI is set
resize                        frame is wider than ``target_width``
grayscale                     ``enable_grayscale``
CLAHE contrast                mean luminance below ``clahe_dark_trigger``
denoise (fastNlMeansDenoising) Laplacian variance above ``denoise_trigger``
adaptive threshold            ``enable_adaptive_threshold`` **and** the frame is
                              bimodal **and** the previous decode failed
sharpen                       ``enable_sharpen``
============================  ==========================================

``PreprocessResult`` reports which steps ran and how long each took; the scanner
exposes that in the frame statistics so an A/B test can be reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.utils.config import PreprocessingConfig, get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PreprocessResult:
    image: np.ndarray
    steps: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "timings_ms": {k: round(v, 3) for k, v in self.timings_ms.items()},
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "total_ms": round(self.total_ms, 3),
        }


def roi_rect(width: int, height: int, roi: Any) -> tuple[int, int, int, int]:
    """Convert a fractional ROI into pixel coordinates, clamped to the frame."""
    x = int(max(0.0, roi.roi_x) * width)
    y = int(max(0.0, roi.roi_y) * height)
    w = int(max(0.0, roi.roi_w) * width)
    h = int(max(0.0, roi.roi_h) * height)
    x = min(x, max(0, width - 1))
    y = min(y, max(0, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


class ImagePreprocessor:
    def __init__(
        self,
        config: PreprocessingConfig | None = None,
        camera_config: Any = None,
    ) -> None:
        self.config = config or get_config().preprocessing
        self.camera = camera_config or get_config().camera
        # Set by the pipeline after a failed decode so the next frame tries the
        # heavier thresholding path (ADR-007).
        self.force_aggressive = False

    # -- statistics --------------------------------------------------------
    @staticmethod
    def mean_luminance(gray: np.ndarray) -> float:
        return float(gray.mean()) / 255.0

    @staticmethod
    def noise_energy(gray: np.ndarray) -> float:
        """Laplacian variance - the standard focus/sharpness proxy.

        Values well above zero mean *either* a sharp edge or sensor noise.  A
        blurred frame also lowers it, which is why the denoise trigger is
        combined with a dark/bimodal check in :meth:`process`.
        """
        import cv2

        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def bimodality(gray: np.ndarray, bins: int = 32) -> float:
        """How strongly the histogram is split into a dark and a light mode.

        1.0 = perfectly bimodal (a good, high-contrast scan), 0.0 = uniform.
        Used to decide whether adaptive thresholding is safe.
        """
        import cv2

        histogram = cv2.calcHist([gray], [0], None, [bins], [0, 256]).flatten()
        total = histogram.sum()
        if total <= 0:
            return 0.0
        probability = histogram / total
        nonzero = probability[probability > 0]
        if nonzero.size < 2:
            return 0.0
        return float(_peak_separation(nonzero))

    # -- main entry point --------------------------------------------------
    def process(self, frame: np.ndarray, *, grayscale_only: bool = False) -> PreprocessResult:
        import time

        import cv2

        started = time.perf_counter()
        result = PreprocessResult(image=frame)
        cfg = self.config
        image = frame

        # 1. ROI crop --------------------------------------------------------
        if cfg.enable_roi_crop:
            height, width = image.shape[:2]
            x, y, w, h = roi_rect(width, height, self.camera)
            if (x, y, w, h) != (0, 0, width, height):
                image = image[y : y + h, x : x + w]
                result.steps.append("roi_crop")
                result.metrics["roi"] = w * h / float(width * height)

        # 2. resize ----------------------------------------------------------
        height, width = image.shape[:2]
        if cfg.target_width and width > cfg.target_width:
            scale = cfg.target_width / float(width)
            image = cv2.resize(
                image, (cfg.target_width, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            result.steps.append("resize")

        # 3. grayscale -------------------------------------------------------
        if cfg.enable_grayscale and image.ndim == 3:
            step = time.perf_counter()
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            result.steps.append("grayscale")
            result.timings_ms["grayscale"] = (time.perf_counter() - step) * 1000.0

        if image.ndim != 2:
            result.image = image
            result.total_ms = (time.perf_counter() - started) * 1000.0
            return result

        # 4. statistics-driven filters --------------------------------------
        mean_lum = self.mean_luminance(image)
        noise = self.noise_energy(image)
        result.metrics["mean_luminance"] = mean_lum
        result.metrics["noise_energy"] = noise

        if cfg.enable_clahe and mean_lum < cfg.clahe_dark_trigger:
            step = time.perf_counter()
            clahe = cv2.createCLAHE(clipLimit=cfg.clahe_clip_limit, tileGridSize=(8, 8))
            image = clahe.apply(image)
            result.steps.append("clahe")
            result.timings_ms["clahe"] = (time.perf_counter() - step) * 1000.0

        if cfg.enable_denoise and noise > cfg.denoise_trigger and mean_lum < 0.75:
            step = time.perf_counter()
            image = cv2.fastNlMeansDenoising(
                image, None, cfg.denoise_h, max(5, int(cfg.denoise_h))
            )
            result.steps.append("denoise")
            result.timings_ms["denoise"] = (time.perf_counter() - step) * 1000.0

        bimodal = self.bimodality(image)
        result.metrics["bimodality"] = bimodal
        if not grayscale_only:
            if cfg.enable_adaptive_threshold or self.force_aggressive:
                if self.force_aggressive or bimodal >= cfg.adaptive_bimodal_threshold:
                    step = time.perf_counter()
                    block = cfg.adaptive_block_size
                    if block % 2 == 0:
                        block += 1
                    image = cv2.adaptiveThreshold(
                        image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                        block, cfg.adaptive_c,
                    )
                    result.steps.append("adaptive_threshold")
                    result.timings_ms["adaptive_threshold"] = (
                        time.perf_counter() - step
                    ) * 1000.0

            if cfg.enable_sharpen:
                step = time.perf_counter()
                blurred = cv2.GaussianBlur(image, (0, 0), 1.0)
                image = cv2.addWeighted(image, 1.0 + cfg.sharpen_amount, blurred,
                                        -cfg.sharpen_amount, 0)
                result.steps.append("sharpen")
                result.timings_ms["sharpen"] = (time.perf_counter() - step) * 1000.0

        result.image = image
        result.total_ms = (time.perf_counter() - started) * 1000.0
        return result


def _peak_separation(probability: np.ndarray) -> float:
    """Separation between the darkest and lightest significant modes."""
    threshold = probability[probability > probability.max() * 0.15]
    if threshold.size < 2:
        return 0.0
    dark = threshold[: threshold.size // 2]
    light = threshold[threshold.size // 2 :]
    return float(light.mean() - dark.mean())


def annotate(
    frame: np.ndarray,
    camera_config: Any,
    detections: list[dict[str, Any]],
    *,
    status_text: str = "",
    status_colour: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """Draw the ROI, detection boxes and a status banner on a copy of the frame.

    Kept separate from the pipeline so the exact same overlay can be produced in
    a unit test with a synthetic frame.
    """
    import cv2

    canvas = frame.copy()
    if canvas.ndim == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    height, width = canvas.shape[:2]
    x, y, w, h = roi_rect(width, height, camera_config)
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (60, 200, 255), 2)
    label = "SCAN AREA"
    cv2.putText(canvas, label, (x + 8, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (60, 200, 255), 1, cv2.LINE_AA)

    for detection in detections:
        points = detection.get("points") or []
        if len(points) >= 4:
            pts = np.array(points, dtype=np.int32)
            colour = detection.get("colour", (80, 220, 120))
            cv2.polylines(canvas, [pts], True, colour, 2)
            top_left = points[0]
            text = str(detection.get("text", ""))
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(canvas, (top_left[0], max(0, top_left[1] - th - 6)),
                          (top_left[0] + tw + 6, top_left[1]), colour, -1)
            cv2.putText(canvas, text, (top_left[0] + 3, max(th + 2, top_left[1] - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    if status_text:
        (tw, th), _ = cv2.getTextSize(status_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(canvas, (0, 0), (tw + 20, th + 16), (20, 20, 20), -1)
        cv2.putText(canvas, status_text, (10, th + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    status_colour, 2, cv2.LINE_AA)
    return canvas
