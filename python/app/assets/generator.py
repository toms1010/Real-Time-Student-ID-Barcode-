"""Generation of real test assets: barcode images and mock ID cards.

These are *generated*, not photographed, and they are what makes the test suite
possible without physical cards:

* ``assets/test-barcodes/``  - one real, scannable barcode image per demo student
  plus a set of symbologies the system must handle or reject gracefully.
* ``assets/sample-id-cards/`` - mock ID cards with the barcode printed on them,
  used for the lighting/distance study and for the demonstration.

Encoding uses ``zxingcpp.create_barcode`` (the same ZXing-C++ library the
scanner links against), so a generated barcode decodes to exactly the payload
that was written - the test asserts a round trip rather than trusting the file
name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.utils.config import PROJECT_ROOT, get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: (filename, payload, symbology) for symbology coverage.
#: The negative cases matter as much as the positive ones: a PDF417 or MaxiCode
#: must be reported as UNSUPPORTED_FORMAT rather than silently resolving.
FORMAT_MATRIX: list[tuple[str, str, str]] = [
    ("code128_primary", "2026-000001", "Code128"),
    ("code128_numeric", "2026000001", "Code128"),
    ("code39_legacy", "ID-2026-000001", "Code39"),
    ("ean13_retail", "5901234123457", "EAN13"),
    ("ean8_retail", "96385074", "EAN8"),
    ("upca_retail", "036000291452", "UPCA"),
    ("upce_retail", "01234565", "UPCE"),
    ("itf_industrial", "1234567890", "ITF"),
    ("qrcode_matrix", "2026-000002", "QRCode"),
    ("datamatrix_matrix", "2026-000003", "DataMatrix"),
    ("unsupported_pdf417", "2026-000004", "PDF417"),
    ("unsupported_aztec", "2026-000005", "Aztec"),
]

CARD_TEMPLATE = {
    "width": 1200,      # mock CR80 proportion (85.6 x 54 mm) at high resolution
    "height": 660,
    "margin": 56,
}


def _render_barcode(payload: str, symbology: str, width: int = 720, height: int = 220):
    """Return a grayscale numpy image of a real barcode."""
    import numpy as np
    import zxingcpp

    try:
        fmt = zxingcpp.barcode_format_from_str(symbology)
    except Exception:  # noqa: BLE001
        raise ValueError(f"unsupported symbology: {symbology}")
    barcode = zxingcpp.create_barcode(payload, fmt)
    image = np.array(barcode.to_image())
    import cv2

    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_NEAREST)
    # The quiet zone is mandatory: without it ZXing cannot find the start guard.
    quiet = 24
    canvas = np.full((resized.shape[0] + quiet * 2, resized.shape[1] + quiet * 2), 255, np.uint8)
    canvas[quiet : quiet + resized.shape[0], quiet : quiet + resized.shape[1]] = resized
    return canvas


def generate_barcodes(output_dir: Path, student_numbers: list[str] | None = None) -> list[Path]:
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for name, payload, symbology in FORMAT_MATRIX:
        path = output_dir / f"{name}.png"
        cv2.imwrite(str(path), _render_barcode(payload, symbology))
        written.append(path)
        logger.info("wrote %s (%s: %s)", path.name, symbology, payload)

    for number in student_numbers or []:
        path = output_dir / f"student_{number}.png"
        cv2.imwrite(str(path), _render_barcode(number, "Code128"))
        written.append(path)
    return written


def render_id_card(
    student: dict[str, Any],
    barcode_image,
    *,
    lighting: str = "normal",
    glare: bool = False,
) -> Any:
    """Compose a mock ID card (BGR image) with the barcode printed on it.

    ``lighting`` and ``glare`` reproduce the conditions in the evaluation table
    so the lighting study can be repeated with the same synthetic input.
    """
    import cv2
    import numpy as np

    width, height, margin = CARD_TEMPLATE["width"], CARD_TEMPLATE["height"], CARD_TEMPLATE["margin"]
    card = np.full((height, width, 3), 248, np.uint8)

    # Header band
    cv2.rectangle(card, (0, 0), (width, 92), (32, 74, 122), -1)
    cv2.putText(card, "DEMO UNIVERSITY", (margin, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(card, "OFFICIAL STUDENT ID - SAMPLE ONLY", (margin, 76),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 220, 240), 1, cv2.LINE_AA)

    # Photo placeholder
    cv2.rectangle(card, (margin, 130), (margin + 190, 380), (215, 219, 226), -1)
    cv2.putText(card, "PHOTO", (margin + 60, 262), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (120, 130, 150), 1, cv2.LINE_AA)

    # Text block
    x = margin + 220
    y = 168
    rows = [
        ("STUDENT ID", student.get("student_id", "")),
        ("NAME", f"{student.get('first_name','')} {student.get('last_name','')}".strip()),
        ("PROGRAM", student.get("course", "")),
        ("YEAR / SECTION", f"{student.get('year_level','')} - {student.get('section','')}"),
        ("STATUS", str(student.get("status", "active")).upper()),
    ]
    for label, value in rows:
        cv2.putText(card, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (110, 110, 120), 1, cv2.LINE_AA)
        cv2.putText(card, str(value)[:42], (x, y + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.68,
                    (25, 25, 30), 2, cv2.LINE_AA)
        y += 72

    # Barcode area (right-hand column, vertically centred)
    bar_h = 190
    bar_w = int(barcode_image.shape[1] * bar_h / barcode_image.shape[0])
    resized = cv2.resize(barcode_image, (bar_w, bar_h), interpolation=cv2.INTER_NEAREST)
    if resized.ndim == 2:
        resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
    bar_x = width - margin - bar_w - 10
    bar_y = (height - bar_h) // 2
    card[bar_y : bar_y + bar_h, bar_x : bar_x + bar_w] = resized
    (label_w, _), _ = cv2.getTextSize(student.get("student_id", ""),
                                      cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    cv2.putText(card, student.get("student_id", ""),
                (bar_x + max(0, (bar_w - label_w) // 2), bar_y - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(card, "SCAN THIS BARCODE", (bar_x, bar_y + bar_h + 34),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 100), 1, cv2.LINE_AA)

    # Lighting / glare simulation
    if lighting == "bright":
        card = np.clip(card.astype(np.float32) * 1.18 + 18, 0, 255).astype(np.uint8)
    elif lighting == "low":
        card = np.clip(card.astype(np.float32) * 0.55, 0, 255).astype(np.uint8)
    elif lighting == "warm":
        card = np.clip(card.astype(np.float32) * np.array([1.06, 0.98, 0.86]), 0, 255).astype(np.uint8)
    if glare:
        overlay = np.zeros_like(card, dtype=np.float32)
        cv2.ellipse(overlay, (int(width * 0.72), int(height * 0.78)),
                    (int(width * 0.16), int(height * 0.10)), 20, 0, 360, (255, 255, 255), -1)
        card = np.clip(card * 0.82 + overlay, 0, 255).astype(np.uint8)
    return card


def generate_id_cards(output_dir: Path, students: list[dict[str, Any]]) -> list[Path]:
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for student in students:
        barcode = _render_barcode(student["student_id"], "Code128", width=760, height=200)
        for lighting, glare, suffix in (
            ("normal", False, ""),
            ("bright", False, "_bright"),
            ("low", False, "_low"),
            ("normal", True, "_glare"),
        ):
            card = render_id_card(student, barcode, lighting=lighting, glare=glare)
            path = output_dir / f"card_{student['student_id']}{suffix}.png"
            cv2.imwrite(str(path), card)
            written.append(path)
    logger.info("wrote %d sample ID card images", len(written))
    return written


def generate_sounds(output_dir: Path) -> list[Path]:
    """Synthesise the four notification tones as real 8-bit mono WAV files.

    Generated rather than committed as binaries so the repository stays
    text-only; the tones are short, distinct, and quiet enough not to startle a
    cashier.  ``aplay``/``paplay`` play them without blocking the scanner.
    """
    import math
    import struct
    import wave

    output_dir.mkdir(parents=True, exist_ok=True)
    rate = 22050
    tones = {
        "scan_detected": [(1180, 0.06)],
        "verification_success": [(880, 0.08), (1320, 0.10)],
        "unknown_student": [(520, 0.12), (392, 0.16)],
        "error": [(240, 0.18), (180, 0.22)],
    }
    written: list[Path] = []
    for name, sequence in tones.items():
        frames = bytearray()
        for frequency, duration in sequence:
            count = int(rate * duration)
            for index in range(count):
                envelope = min(1.0, index / (rate * 0.01)) * min(
                    1.0, (count - index) / (rate * 0.02)
                )
                value = int(12000 * envelope * math.sin(2 * math.pi * frequency * index / rate))
                frames += struct.pack("<h", value)
        path = output_dir / f"{name}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
        written.append(path)
        logger.info("wrote %s", path.name)
    return written


def generate_all(args: Any = None) -> int:
    """CLI entry point behind ``app.cli generate-assets``."""
    import cv2

    from app.database.repository import StudentRepository

    base = Path(getattr(args, "output", None) or (PROJECT_ROOT / "assets"))
    students_rows, _ = StudentRepository().list(limit=50)
    students = [
        {
            "student_id": s.student_id,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "course": s.course,
            "year_level": s.year_level,
            "section": s.section,
            "status": s.status,
        }
        for s in students_rows
    ]
    if not students:
        print("no students in the database - run 'python -m app.cli db-seed' first")
        return 1

    barcodes = generate_barcodes(base / "test-barcodes",
                                 [s["student_id"] for s in students])
    cards = generate_id_cards(base / "sample-id-cards", students)
    sounds = generate_sounds(base / "sounds")

    # A single image for the repository README / documentation.
    preview = base / "sample-id-cards" / "card_overview.png"
    grid = _contact_sheet([str(base / "sample-id-cards" / f"card_{s['student_id']}.png")
                           for s in students[:4]])
    cv2.imwrite(str(preview), grid)

    print(f"barcodes : {len(barcodes)} files in {base / 'test-barcodes'}")
    print(f"cards    : {len(cards)} files in {base / 'sample-id-cards'}")
    print(f"sounds   : {len(sounds)} files in {base / 'sounds'}")
    print(f"contact sheet: {preview}")
    return 0


def _contact_sheet(paths: list[str]) -> Any:
    import cv2
    import numpy as np

    tiles = []
    for path in paths:
        image = cv2.imread(path)
        if image is not None:
            tiles.append(cv2.resize(image, (640, 403)))
    if not tiles:
        return np.zeros((10, 10, 3), np.uint8)
    while len(tiles) % 2:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i : i + 2]) for i in range(0, len(tiles), 2)]
    return np.vstack(rows)
