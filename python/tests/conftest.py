"""Shared pytest fixtures.

Every test runs against a **temporary database**, never the real one:

* `config` points the application at a throwaway SQLite file in ``tmp_path``
* `repository` is a fresh :class:`Repository` over that file
* `app` / `client` are a FastAPI instance and ``TestClient`` over it

That isolation is why the suite is safe to run while a development server is
running against the real database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

# The scanner must never reach a real camera or a real network during tests.
os.environ.setdefault("SIDB_PROJECT_ROOT", str(PROJECT_ROOT))
os.environ.setdefault("APP_ENV", "testing")

from app.database.connection import Database, set_database  # noqa: E402
from app.database.migrations import apply_migrations  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.report_service import ReportService  # noqa: E402
from app.services.scan_service import ScanService  # noqa: E402
from app.services.settings_service import SettingsService  # noqa: E402
from app.services.student_service import StudentService  # noqa: E402
from app.services.transaction_service import TransactionService  # noqa: E402
from app.services.verification_service import VerificationService  # noqa: E402
from app.utils.config import load_config  # noqa: E402
from app.utils.security import DEMO_PASSWORD, hash_password  # noqa: E402

TEST_PASSWORD = DEMO_PASSWORD


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def barcode_assets(project_root: Path) -> dict[str, Path]:
    """The generated barcode fixtures, or skip with an actionable message."""
    directory = project_root / "assets" / "test-barcodes"
    if not (directory / "code128_primary.png").is_file():
        pytest.skip(
            "barcode fixtures are missing - run: "
            "source .venv/bin/activate && python -m app.cli db-seed && "
            "python -m app.cli generate-assets"
        )
    return {path.stem: path for path in directory.glob("*.png")}


@pytest.fixture(scope="session")
def id_cards(project_root: Path) -> list[Path]:
    directory = project_root / "assets" / "sample-id-cards"
    if not (directory / "card_2026-000001.png").is_file():
        pytest.skip(
            "sample ID cards are missing - run: python -m app.cli generate-assets"
        )
    return sorted(directory.glob("card_*.png"))


@pytest.fixture(scope="session")
def demo_config(tmp_path_factory: pytest.TempPathFactory):
    """A configuration whose database and uploads live in a temp directory."""
    root = tmp_path_factory.mktemp("sidb-config")
    config = load_config(
        env_file=None,  # do not read the developer's .env
        overrides={
            "database.path": str(root / "test.db"),
            "uploads.photo_dir": str(root / "photos"),
            "reports.output_dir": str(root / "exports"),
            "reports.timezone": "UTC",
            "application.environment": "testing",
            "scanner.cooldown_ms": 2000,
            "security.session_ttl_minutes": 30,
        },
    )
    return config


@pytest.fixture()
def database(demo_config) -> Database:
    """A fresh, migrated database per test."""
    path = demo_config.resolve(demo_config.database.path)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()

    db = Database(path, busy_timeout_ms=3000, enable_wal=True)
    db.execute_script(demo_config.schema_path.read_text(encoding="utf-8"))
    apply_migrations(db)
    set_database(db)
    yield db
    db.close()
    set_database(None)


@pytest.fixture()
def repository(database) -> Repository:
    return Repository(database)


@pytest.fixture()
def config(demo_config):
    return demo_config


@pytest.fixture()
def students(repository, config) -> StudentService:
    return StudentService(repo=repository, config=config)


@pytest.fixture()
def verification(students, config) -> VerificationService:
    return VerificationService(students=students, config=config)


@pytest.fixture()
def scans(repository, verification, config) -> ScanService:
    return ScanService(repo=repository, verification=verification, config=config)


@pytest.fixture()
def transactions(repository, scans) -> TransactionService:
    service = TransactionService(repo=repository)
    service.bind_cooldown(scans.cooldowns)
    return service


@pytest.fixture()
def reports(repository, config) -> ReportService:
    return ReportService(repo=repository, config=config)


@pytest.fixture()
def settings(repository, config) -> SettingsService:
    return SettingsService(repo=repository, config=config)


@pytest.fixture()
def auth(repository, config) -> AuthService:
    return AuthService(repo=repository, config=config)


# ---------------------------------------------------------------------------
# seeded data
# ---------------------------------------------------------------------------
DEMO_STUDENTS = [
    {
        "student_id": "2026-000001", "first_name": "Juan", "middle_name": "Ramirez",
        "last_name": "Dela Cruz", "course": "BS Computer Engineering", "year_level": 3,
        "section": "A", "status": "active",
    },
    {
        "student_id": "2026-000002", "first_name": "Maria", "middle_name": "Lopez",
        "last_name": "Santos", "course": "BS Computer Engineering", "year_level": 3,
        "section": "A", "status": "active",
    },
    {
        "student_id": "2026-000003", "first_name": "Angelo", "last_name": "Reyes",
        "course": "BS Information Technology", "year_level": 2, "section": "B",
        "status": "active",
    },
    {
        "student_id": "2026-000004", "first_name": "Kristine", "middle_name": "Bautista",
        "last_name": "Garcia", "course": "BS Nursing", "year_level": 3, "section": "C",
        "status": "active",
    },
    {
        "student_id": "2026-000007", "first_name": "Carlo", "last_name": "Aquino",
        "course": "BS Information Technology", "year_level": 4, "section": "C",
        "status": "inactive",
    },
    {
        "student_id": "2026-000012", "first_name": "Hannah", "middle_name": "Lim",
        "last_name": "Salazar", "course": "BS Accountancy", "year_level": 4,
        "section": "A", "status": "suspended",
    },
]


@pytest.fixture()
def demo_students(students: StudentService) -> list:
    """Six fictional students plus their primary Code 128 barcodes."""
    created = []
    for payload in DEMO_STUDENTS:
        created.append(students.create(dict(payload)))
    return created


@pytest.fixture()
def demo_users(auth: AuthService) -> dict[str, dict]:
    """An administrator, a cashier and a viewer with known passwords."""
    accounts = {}
    for username, role in (("admin", "admin"), ("cashier", "cashier"), ("viewer", "viewer")):
        user = auth.create_user(
            username=username,
            password=TEST_PASSWORD,
            role=role,
            full_name=f"Test {role}",
            must_change_password=False,
        )
        accounts[username] = {"id": user.id, "role": role, "password": TEST_PASSWORD}
    return accounts


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(demo_config, database, repository):
    """A FastAPI app wired to the temporary database."""
    from app.dependencies import reset_services
    from app.main import create_app

    reset_services()
    application = create_app(demo_config)
    application.state.config = demo_config
    # The lifespan is not run by TestClient unless used as a context manager, so
    # the singletons are pointed at the test database explicitly.
    from app.dependencies import (
        get_auth,
        get_reports,
        get_repo,
        get_scans,
        get_settings,
        get_students,
        get_transactions,
    )

    import app.dependencies as dependencies

    dependencies._repo = repository
    dependencies._auth = get_auth.__wrapped__ if hasattr(get_auth, "__wrapped__") else None  # type: ignore[attr-defined]
    dependencies._auth = AuthService(repo=repository, config=demo_config)
    dependencies._students = StudentService(repo=repository, config=demo_config)
    dependencies._verification = VerificationService(students=dependencies._students, config=demo_config)
    dependencies._scans = ScanService(
        repo=repository, verification=dependencies._verification, config=demo_config
    )
    dependencies._transactions = TransactionService(repo=repository)
    dependencies._transactions.bind_cooldown(dependencies._scans.cooldowns)
    dependencies._reports = ReportService(repo=repository, config=demo_config)
    dependencies._settings = SettingsService(repo=repository, config=demo_config)
    yield application
    reset_services()


@pytest.fixture()
def client(app) -> Iterator:
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client, demo_users) -> dict[str, str]:
    """Authorization headers for each seeded account."""
    headers = {}
    for username, account in demo_users.items():
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": account["password"]},
        )
        assert response.status_code == 200, response.text
        headers[username] = {"Authorization": f"Bearer {response.json()['token']}"}
    return headers


@pytest.fixture()
def admin_headers(auth_headers) -> dict[str, str]:
    return auth_headers["admin"]


@pytest.fixture()
def cashier_headers(auth_headers) -> dict[str, str]:
    return auth_headers["cashier"]


@pytest.fixture()
def seeded(client, demo_users, students) -> dict:
    """A logged-in client with students already present - the common case."""
    response = client.post(
        "/api/auth/login", json={"username": "cashier", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    created = [students.create(dict(payload)) for payload in DEMO_STUDENTS]
    return {
        "client": client,
        "headers": {"Authorization": f"Bearer {response.json()['token']}"},
        "token": response.json()["token"],
        "students": created,
    }


__all__ = [
    "TEST_PASSWORD",
    "DEMO_STUDENTS",
    "hash_password",
    "get_repo",
    "get_scans",
    "get_students",
    "get_transactions",
    "get_settings",
    "get_reports",
]
