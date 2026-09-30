"""Forward-only SQL migrations.

``database/schema.sql`` is the authoritative snapshot used for fresh installs.
Every subsequent change lives in ``database/migrations/NNNN_*.sql`` and is
applied here in lexicographic order, recorded in ``schema_migrations``.

The applier is idempotent and safe to run on every start-up, which is what
``app.cli db-migrate`` and ``scripts/start.sh`` do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.database.connection import Database, get_database
from app.utils.errors import DatabaseError
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class MigrationFile:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def description(self) -> str:
        """First ``--`` comment line after the version marker, if present."""
        for line in self.sql.splitlines():
            stripped = line.strip().lstrip("-").strip()
            if stripped:
                return stripped[:200]
        return self.path.stem


def discover_migrations(directory: Path) -> list[MigrationFile]:
    if not directory.is_dir():
        return []
    files = sorted(
        (p for p in directory.glob("*.sql") if p.stem[:4].isdigit()),
        key=lambda p: p.stem,
    )
    return [MigrationFile(version=p.stem[:4], path=p) for p in files]


def applied_versions(database: Database) -> set[str]:
    try:
        rows = database.query("SELECT version FROM schema_migrations")
    except DatabaseError:
        return set()
    return {str(row["version"]) for row in rows}


def apply_migrations(
    database: Database | None = None, directory: Path | None = None
) -> list[str]:
    """Apply every pending migration.  Returns the versions that were applied."""
    db = database or get_database()
    folder = directory or _migrations_dir(db)

    files = discover_migrations(folder)
    done = applied_versions(db)
    applied: list[str] = []

    # The schema must exist before migrations can be recorded against it.
    if not _has_schema(db):
        _create_schema(db)

    for migration in files:
        if migration.version in done:
            continue
        logger.info("applying migration %s (%s)", migration.version, migration.path.name)
        try:
            # `executescript` implicitly commits, so it must not run inside
            # Database.transaction(); the migration file carries its own
            # BEGIN/COMMIT when it needs atomicity.
            db.execute_script(migration.sql)
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(
                f"migration {migration.version} failed",
                details={"file": migration.path.name, "error": str(exc)},
            ) from exc
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
            (migration.version, migration.description),
        )
        applied.append(migration.version)
    return applied


def migration_status(
    database: Database | None = None, directory: Path | None = None
) -> list[dict[str, str]]:
    """Return ``[{"version", "file", "state"}]`` for every migration file."""
    db = database or get_database()
    folder = directory or _migrations_dir(db)
    done = applied_versions(db)
    status: list[dict[str, str]] = []
    for migration in discover_migrations(folder):
        status.append(
            {
                "version": migration.version,
                "file": migration.path.name,
                "state": "applied" if migration.version in done else "pending",
            }
        )
    return status


def _migrations_dir(db: Database) -> Path:
    from app.utils.config import get_config

    return get_config().migrations_dir


def _has_schema(db: Database) -> bool:
    return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name='students'"))


def _create_schema(db: Database) -> None:
    from app.utils.config import get_config

    schema = get_config().schema_path
    if not schema.is_file():
        raise DatabaseError("database/schema.sql is missing", details={"path": schema.name})
    logger.info("initialising schema from %s", schema.name)
    db.execute_script(schema.read_text(encoding="utf-8"))
