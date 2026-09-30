"""Python-side computer vision.

This package is the **fallback / browser-facing** capture and decode path.  The
production path is the C++ scanner (``cpp/``) - see ADR-006.  Both call the same
``zxing-cpp`` library and the same ``POST /api/scans`` endpoint, so behaviour
does not depend on which one owns the camera.

Responsibilities
----------------
``camera``      V4L2 / file / RTSP capture with reconnect and health reporting
``preprocess``  the same adaptive cascade as ``cpp/src/processing/``
``decoder``     ZXing-C++ wrapper: symbology allow-list, rotation trials
``pipeline``    capture + decode threads, cooldown, statistics, ROI overlay
"""

__all__ = ["camera", "decoder", "pipeline", "preprocess"]
