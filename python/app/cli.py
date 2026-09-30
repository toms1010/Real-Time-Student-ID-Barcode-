"""Command line interface.

Every administrative task that would otherwise need a GUI or a SQL shell::

    python -m app.cli db-init              create the schema
    python -m app.cli db-migrate           apply pending migrations
    python -m app.cli db-seed              load the fictional demo students
    python -m app.cli db-info              print the data dictionary
    python -m app.cli create-admin         create an administrator (password prompt)
    python -m app.cli set-password USER    reset a password
    python -m app.cli list-users
    python -m app.cli backup               consistent SQLite backup
    python -m app.cli generate-assets      render test barcodes and sample ID cards
    python -m app.cli decode IMAGE...      decode one or more images (diagnostics)
    python -m app.cli verify BARCODE       resolve a barcode without recording a scan
    python -m app.cli benchmark            measure the decode + lookup pipeline

``python -m app.cli`` with no arguments prints this list.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from app.utils.config import PROJECT_ROOT, get_config, load_config
from app.utils.errors import AppError
from app.utils.logger import get_logger, setup_logging

logger = get_logger("app.cli")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
def cmd_db_init(args: argparse.Namespace) -> int:
    from app.database.connection import get_database, init_database
    from app.database.migrations import apply_migrations, migration_status

    config = load_config(args.config) if args.config else get_config()
    database = init_database(config)
    applied = apply_migrations(database)
    print(f"database ready: {database.path}")
    print(f"applied migrations this run: {applied or 'none (already up to date)'}")
    for row in migration_status(database):
        print(f"  [{row['state']:>7}] {row['version']}  {row['file']}")
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    from app.database.connection import get_database
    from app.database.migrations import apply_migrations, migration_status

    database = get_database()
    if args.status:
        for row in migration_status(database):
            print(f"[{row['state']:>7}] {row['version']}  {row['file']}")
        return 0
    applied = apply_migrations(database)
    print(f"applied: {applied or 'nothing to do'}")
    return 0


def cmd_db_seed(args: argparse.Namespace) -> int:
    """Load the fictional demo students and hash the demo passwords."""
    from app.database.connection import init_database
    from app.database.repository import StudentRepository, UserRepository
    from app.utils.security import DEMO_PASSWORD, hash_demo_password

    config = get_config()
    database = init_database(config)
    repo_users = UserRepository(database)

    script = config.seed_path
    if not script.is_file():
        print(f"seed file not found: {script}")
        return 1
    # The seed file contains a placeholder for the password hash; substitute a
    # real Argon2id hash of the documented demo password before executing it.
    sql = script.read_text(encoding="utf-8")
    placeholder = "PLACEHOLDER_REPLACED_AT_SEED_TIME"
    if placeholder in sql:
        sql = sql.replace(
            "$argon2id$v=19$m=65536,t=3,p=4$c3R1ZGVudC1kZW1vLXNhbHQ$" + placeholder,
            hash_demo_password(),
        )
    if not args.force and repo_users.count() > 0:
        print("users already exist - nothing to do (use --force to seed anyway)")
        print(f"demo password is: {DEMO_PASSWORD}")
        return 0

    database.execute_script(sql)
    barcodes = database.scalar("SELECT COUNT(*) FROM student_barcodes")
    print(f"seeded {StudentRepository(database).count()} students "
          f"and {barcodes} barcodes into {database.path.name}")
    print(f"demo accounts: admin / cashier / staff   password: {DEMO_PASSWORD}")
    print("the admin account is flagged must_change_password=1")
    return 0


def cmd_db_info(args: argparse.Namespace) -> int:
    from app.database.models import TABLES

    config = get_config()
    print(f"schema: {config.schema_path}\ndatabase: {config.database_path}\n")
    for name, meta in TABLES.items():
        print(f"== {name} == {meta['description']}")
        for column, comment in meta["columns"].items():
            print(f"  {column:<22} {comment}")
        print()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    import sqlite3

    from app.database.connection import get_database
    from app.utils.timeutil import utc_now

    database = get_database()
    if not database.exists:
        print("no database to back up")
        return 1
    target_dir = PROJECT_ROOT / (args.output or "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"student_id_system-{utc_now():%Y%m%d-%H%M%S}.db"
    destination = sqlite3.connect(target)
    try:
        database.connect().backup(destination)
    finally:
        destination.close()
    print(f"backup written: {target}")
    print(f"size: {target.stat().st_size / 1024:.1f} KiB")
    return 0


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.services.auth_service import AuthService

    auth = AuthService()
    username = args.username or input("username [admin]: ").strip() or "admin"
    password = args.password or getpass.getpass("password: ")
    if args.password_from_env:
        password = os.environ[args.password_from_env]
    confirm = args.password or getpass.getpass("confirm password: ")
    if password != confirm:
        print("passwords do not match")
        return 1
    try:
        user = auth.create_user(
            username=username,
            password=password,
            role="admin" if args.role == "admin" else args.role,
            full_name=args.full_name,
            must_change_password=not args.no_must_change,
        )
    except AppError as exc:
        print(f"error: {exc.message}")
        return 1
    print(f"created {user.username} (id={user.id}, role={user.role})")
    return 0


def cmd_set_password(args: argparse.Namespace) -> int:
    from app.database.repository import UserRepository
    from app.utils.security import hash_password

    repo = UserRepository()
    user = repo.get_by_username(args.username)
    if user is None:
        print(f"no such user: {args.username}")
        return 1
    password = args.password or getpass.getpass("new password: ")
    repo.set_password(user.id or 0, hash_password(password), must_change=args.must_change)
    repo.revoke_all_sessions(user.id or 0)
    print(f"password updated for {args.username}; all sessions revoked")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    from app.database.repository import UserRepository

    users = UserRepository().list()
    if not users:
        print("no users yet")
        return 0
    print(f"{'ID':>4}  {'USERNAME':<20} {'ROLE':<9} {'ACTIVE':<7} LAST LOGIN")
    for user in users:
        print(f"{user.id:>4}  {user.username:<20} {user.role:<9} "
              f"{'yes' if user.is_active else 'no':<7} {user.last_login_at or '-'}")
    return 0


# ---------------------------------------------------------------------------
# assets / diagnostics
# ---------------------------------------------------------------------------
def cmd_generate_assets(args: argparse.Namespace) -> int:
    from app.assets.generator import generate_all

    return generate_all(args)


def cmd_decode(args: argparse.Namespace) -> int:
    """Decode image files with the same decoder the scanner uses."""
    from app.vision.decoder import BarcodeDecoder

    decoder = BarcodeDecoder()
    failures = 0
    for path in args.images:
        result = decoder.decode_image_file(path)
        if not result.found:
            failures += 1
            print(f"{path}: {result.error_code} ({result.duration_ms:.1f} ms)")
            continue
        for detection in result.detections:
            print(f"{path}: {detection.text!r} format={detection.format} "
                  f"valid={detection.valid} error={detection.error_type} "
                  f"({result.duration_ms:.1f} ms)")
    print(f"\n{len(args.images) - failures}/{len(args.images)} decoded")
    return 1 if failures and args.strict else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Resolve a barcode through the verification service without logging it."""
    from app.services.verification_service import VerificationService

    service = VerificationService()
    for value in args.barcodes:
        outcome = service.verify(value, barcode_type=args.barcode_type)
        student = outcome.student
        print(f"{value}: {outcome.result}")
        print(f"  message : {outcome.message}")
        if student:
            print(f"  student : {student.full_name} ({student.student_id}) "
                  f"{student.course or ''} year {student.year_level} "
                  f"section {student.section} status={student.status}")
        print(f"  db time : {outcome.database_time_ms:.2f} ms")
    return 0


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------
def cmd_benchmark(args: argparse.Namespace) -> int:
    from app.vision.benchmark import run_benchmark

    return run_benchmark(args)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description="Student ID Barcode System - administration and diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="path to a YAML configuration file")
    parser.add_argument("--log-level", default=None, help="override the log level")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("db-init", help="create the schema and apply migrations")
    p.set_defaults(func=cmd_db_init)

    p = sub.add_parser("db-migrate", help="apply pending migrations")
    p.add_argument("--status", action="store_true", help="list versions instead of applying")
    p.set_defaults(func=cmd_db_migrate)

    p = sub.add_parser("db-seed", help="load the fictional demo students")
    p.add_argument("--force", action="store_true", help="seed even if users already exist")
    p.set_defaults(func=cmd_db_seed)

    p = sub.add_parser("db-info", help="print the data dictionary")
    p.set_defaults(func=cmd_db_info)

    p = sub.add_parser("backup", help="write a consistent database backup")
    p.add_argument("--output", help="output directory (default: backups/)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("create-admin", help="create an administrator account")
    p.add_argument("--username", help="username (prompted when omitted)")
    p.add_argument("--password", help="password (discouraged: visible in the shell history)")
    p.add_argument("--password-from-env", metavar="VAR",
                   help="read the password from an environment variable")
    p.add_argument("--full-name", help="display name")
    p.add_argument("--role", default="admin", help="admin | cashier | staff | operator | viewer")
    p.add_argument("--no-must-change", action="store_true",
                   help="do not require a password change at first sign-in")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("set-password", help="reset a user's password")
    p.add_argument("username")
    p.add_argument("--password", help="new password (prompted when omitted)")
    p.add_argument("--must-change", action="store_true",
                   help="force a change at next sign-in")
    p.set_defaults(func=cmd_set_password)

    p = sub.add_parser("list-users", help="list user accounts")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("generate-assets", help="render test barcodes and sample ID cards")
    p.add_argument("--output", help="output directory (default: assets/)")
    p.add_argument("--format", default="Code128", help="symbology for the test barcodes")
    p.set_defaults(func=cmd_generate_assets)

    p = sub.add_parser("decode", help="decode images with the production decoder")
    p.add_argument("images", nargs="+", help="image files to decode")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any image fails")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("verify", help="resolve a barcode without recording a scan")
    p.add_argument("barcodes", nargs="+")
    p.add_argument("--barcode-type", help="symbology to assume, e.g. Code128")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("benchmark", help="measure decode and lookup performance")
    p.add_argument("--images", nargs="*", help="images or a directory to decode repeatedly")
    p.add_argument("--iterations", type=int, default=50, help="decodes per image")
    p.add_argument("--barcode", help="also measure the database lookup for this payload")
    p.add_argument("--output", help="write the JSON result to this file")
    p.set_defaults(func=cmd_benchmark)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging((args.log_level or "WARNING").upper(), "text", force=True)
    try:
        return int(args.func(args) or 0)
    except AppError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
