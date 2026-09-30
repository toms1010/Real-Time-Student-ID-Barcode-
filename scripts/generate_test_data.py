#!/usr/bin/env python3
"""Generate demo data: students, barcodes, ID cards and notification sounds.

A thin wrapper around ``python -m app.cli`` so the documented commands work
from a shell as well as from Python.

    ./scripts/generate_test_data.py --students 40
    ./scripts/generate_test_data.py --barcodes-only
    ./scripts/generate_test_data.py --verify

Every record is clearly fictional: names are assembled from a fixed syllable
list, the domain is the reserved ``example.test``, and phone numbers use the
555-01xx range reserved for fiction (RFC 2606 / ITU E.164 fiction block).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "python"))
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT / "python"))

FIRST_NAMES = [
    "Juan", "Maria", "Angelo", "Kristine", "Paolo", "Bea", "Carlo", "Sofia",
    "Nico", "Trisha", "Miguel", "Hannah", "Rafael", "Camille", "Diego", "Nicole",
    "Andrea", "Francis", "Grace", "Luis",
]
LAST_NAMES = [
    "Dela Cruz", "Santos", "Reyes", "Garcia", "Mendoza", "Villanueva", "Aquino",
    "Domingo", "Farolano", "Alvarez", "Buenaventura", "Salazar", "Bautista",
    "Lopez", "Ramos", "Cordero", "Navarro", "Pascual", "Tolentino", "Zamora",
]
COURSES = [
    "BS Computer Engineering",
    "BS Information Technology",
    "BS Business Administration",
    "BS Accountancy",
    "BS Nursing",
    "BS Mechanical Engineering",
    "BS Civil Engineering",
    "BS Hospitality Management",
]
COLLEGES = [
    "College of Engineering",
    "College of Computing",
    "School of Business",
    "College of Allied Health",
]
SECTIONS = ["A", "B", "C", "D"]


def make_students(count: int, start: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    students = []
    for index in range(start, start + count):
        status = "active"
        roll = rng.random()
        if roll > 0.94:
            status = "suspended"
        elif roll > 0.88:
            status = "inactive"
        elif roll > 0.84:
            status = "graduated"
        students.append(
            {
                "student_id": f"2026-{index:06d}",
                "first_name": rng.choice(FIRST_NAMES),
                "middle_name": rng.choice(["", "Ramirez", "Lopez", "Tan", "Cruz", "Ocampo"]),
                "last_name": rng.choice(LAST_NAMES),
                "course": rng.choice(COURSES),
                "year_level": rng.randint(1, 4),
                "section": rng.choice(SECTIONS),
                "school": rng.choice(COLLEGES),
                "email": f"student{index:06d}@example.test",
                "phone": f"+63-555-{1000 + index % 100:04d}",
                "status": status,
            }
        )
    return students


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--students", type=int, default=40,
                        help="how many demo students to create (default: 40)")
    parser.add_argument("--start", type=int, default=1,
                        help="first student number to allocate (default: 1)")
    parser.add_argument("--seed", type=int, default=2026, help="RNG seed for reproducibility")
    parser.add_argument("--output", default=None, help="asset directory (default: assets/)")
    parser.add_argument("--barcodes-only", action="store_true",
                        help="only regenerate the barcode and ID-card images")
    parser.add_argument("--verify", action="store_true",
                        help="decode every generated image and report the result")
    args = parser.parse_args(argv)

    from app.database.repository import Repository  # noqa: E402
    from app.utils.config import get_config  # noqa: E402
    from app.utils.logger import setup_logging  # noqa: E402

    setup_logging("WARNING", "text", force=True)
    config = get_config()

    # 1. database records ----------------------------------------------------
    if not args.barcodes_only:
        repo = Repository()
        students = make_students(args.students, args.start, args.seed)
        created = skipped = 0
        for payload in students:
            if repo.students.get_by_student_id(payload["student_id"]) is not None:
                skipped += 1
                continue
            try:
                repo.students.create(dict(payload))
                created += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not create {payload['student_id']}: {exc}")
        print(f"students: created {created}, skipped {skipped} (already present)")
    else:
        repo = Repository()

    # 2. assets --------------------------------------------------------------
    from app.assets.generator import generate_all  # noqa: E402

    class _Args:
        output = args.output or str(config.resolve("assets"))
        format = "Code128"

    generate_all(_Args())

    # 3. optional verification ----------------------------------------------
    if args.verify:
        from app.vision.decoder import BarcodeDecoder  # noqa: E402

        decoder = BarcodeDecoder(config)
        cards = sorted((config.resolve("assets") / "sample-id-cards").glob("*.png"))
        ok = failed = 0
        for path in cards:
            result = decoder.decode_image_file(str(path))
            if result.found and result.best() and result.best().valid:
                ok += 1
            else:
                failed += 1
                print(f"  ! could not decode {path.name}")
        print(f"decoded {ok}/{len(cards)} sample ID cards")
        if failed:
            print("  (the generated cards are a test fixture; run generate-assets to rebuild)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
