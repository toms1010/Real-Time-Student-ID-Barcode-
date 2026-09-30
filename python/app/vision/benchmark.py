"""Performance benchmark harness.

Produces the numbers that fill the ``[MEASURE]`` placeholders in
``docs/performance-evaluation.md``.  Everything it prints is measured on the
machine it runs on; nothing is hard-coded, and no value is estimated.

What is measured
----------------
* ``decode_ms``            - preprocess + ZXing-C++ decode of a real image
* ``lookup_ms``            - the API-side lookup path (validate -> query -> build)
* ``total_ms``             - decode + lookup, i.e. the cashier-visible latency
* throughput (FPS)         - decode attempts per second on one image
* detection rate           - how many of the supplied images decoded

Usage::

    python -m app.cli benchmark --images assets/sample-id-cards --iterations 30
    python -m app.cli benchmark --barcode 2026-000001 --iterations 200
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.utils.config import get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def _collect_images(paths: list[str]) -> list[Path]:
    images: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            images.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES))
        elif path.is_file():
            images.append(path)
    return images


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
        "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
    }


def run_benchmark(args: Any = None) -> int:
    from app.vision.decoder import BarcodeDecoder
    from app.vision.preprocess import ImagePreprocessor

    config = get_config()
    iterations = int(getattr(args, "iterations", 50) or 50)
    decoder = BarcodeDecoder(config)
    preprocessor = ImagePreprocessor(config.preprocessing, config.camera)

    results: dict[str, Any] = {
        "environment": {
            "python": __import__("platform").python_version(),
            "opencv": _opencv_version(),
            "zxing_cpp": _zxing_version(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "configuration": {
            "target_width": config.preprocessing.target_width,
            "preprocessing": {
                "grayscale": config.preprocessing.enable_grayscale,
                "roi_crop": config.preprocessing.enable_roi_crop,
                "clahe": config.preprocessing.enable_clahe,
                "denoise": config.preprocessing.enable_denoise,
                "adaptive_threshold": config.preprocessing.enable_adaptive_threshold,
            },
        },
        "iterations_per_image": iterations,
    }

    images = _collect_images(list(getattr(args, "images", None) or []))
    if images:
        import cv2

        decode_times: list[float] = []
        preprocess_times: list[float] = []
        per_image: list[dict[str, Any]] = []
        detected = 0

        for path in images:
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            # Warm-up: the first call pays for lazy library initialisation.
            preprocessor.process(frame)
            decoder.decode(frame)

            hits = 0
            times: list[float] = []
            for _ in range(iterations):
                started = time.perf_counter()
                pre = preprocessor.process(frame)
                result = decoder.decode(pre.image)
                elapsed = (time.perf_counter() - started) * 1000.0
                times.append(elapsed)
                if result.found:
                    hits += 1
            decode_times.extend(times)
            if hits:
                detected += 1
            per_image.append(
                {
                    "file": path.name,
                    "resolution": f"{frame.shape[1]}x{frame.shape[0]}",
                    "detected": bool(hits),
                    "detection_rate": round(hits / iterations, 4),
                    "decode_ms": _stats(times),
                }
            )

        results["decode"] = {
            "images": len(per_image),
            "images_detected": detected,
            "detection_rate": round(detected / len(per_image), 4) if per_image else None,
            "total_ms": _stats(decode_times),
            "throughput_per_second": (
                round(1000.0 / statistics.fmean(decode_times), 2) if decode_times else None
            ),
            "per_image": per_image,
        }
        print(f"decoded {detected}/{len(per_image)} images, "
              f"mean {results['decode']['total_ms'].get('mean', 0):.2f} ms "
              f"({results['decode']['throughput_per_second']} decodes/s)")

    barcode = getattr(args, "barcode", None)
    if barcode:
        from app.services.verification_service import VerificationService

        service = VerificationService(config=config)
        service.verify(barcode)  # warm the SQLite page cache
        lookup_times: list[float] = []
        for _ in range(max(10, iterations)):
            started = time.perf_counter()
            outcome = service.verify(barcode, barcode_type="Code128")
            lookup_times.append((time.perf_counter() - started) * 1000.0)
        results["lookup"] = {
            "barcode": barcode,
            "result": outcome.result,
            "total_ms": _stats(lookup_times),
            "throughput_per_second": round(1000.0 / statistics.fmean(lookup_times), 2),
        }
        print(f"lookup {barcode} -> {outcome.result} in "
              f"{results['lookup']['total_ms']['mean']:.3f} ms mean")

    if images and barcode:
        results["end_to_end"] = {
            "mean_ms": round(
                results["decode"]["total_ms"]["mean"] + results["lookup"]["total_ms"]["mean"], 3
            ),
            "budget_ms": 500.0,
            "meets_budget": (
                results["decode"]["total_ms"]["mean"] + results["lookup"]["total_ms"]["mean"] < 500.0
            ),
        }
        print(f"end-to-end mean {results['end_to_end']['mean_ms']:.2f} ms "
              f"(budget {results['end_to_end']['budget_ms']} ms)")

    output = getattr(args, "output", None)
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(json.dumps({k: v for k, v in results.items() if k != "decode"}, indent=2))
    return 0


def _opencv_version() -> str:
    try:
        import cv2

        return cv2.__version__
    except Exception:  # noqa: BLE001 # pragma: no cover
        return "unavailable"


def _zxing_version() -> str:
    try:
        import zxingcpp

        return getattr(zxingcpp, "__version__", "installed")
    except Exception:  # noqa: BLE001 # pragma: no cover
        return "unavailable"
