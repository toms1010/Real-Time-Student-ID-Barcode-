"""SQLite connection management.

Design points
-------------
* **One connection per thread.**  ``sqlite3`` objects are not safe to share
  across threads, and the service runs camera, MJPEG and request threads
  concurrently.  Connections are created lazily per thread and closed on
  interpreter shutdown.
* **WAL + busy timeout.**  Readers (the scanner polling the API) must not block
  the writer (a scan being recorded).  ``journal_mode=WAL`` gives us that;
  ``busy_timeout`` makes concurrent writers wait instead of raising
  ``database is locked`` immediately.
* **Foreign keys are enforced.**  SQLite defaults them to *off*; without this
  line the ``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` rules in the schema
  are documentation rather than behaviour.
* **Statements are always parameterised.**  ``Database.execute`` accepts
  ``params`` only; there is no string-interpolation entry point in this module.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.utils.config import AppConfig, get_config
from app.utils.errors import DatabaseError
from app.utils.logger import get_logger
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)

Row = dict[str, Any]


class Database:
    """Thread-safe SQLite wrapper bound to one database file."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000, enable_wal: bool = True):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.enable_wal = enable_wal
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()

    # -- connection lifecycle ---------------------------------------------
    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,  # explicit BEGIN/COMMIT, see transaction()
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(
                "could not open the student database",
                details={"hint": "check file permissions on database/"},
            ) from exc

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        if self.enable_wal:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error:  # pragma: no cover - e.g. network filesystem
                logger.warning("could not enable WAL journalling for %s", self.path.name)
        self._local.conn = conn
        with self._connections_lock:
            self._all_connections.append(conn)
        logger.debug("opened SQLite connection for thread %s", threading.current_thread().name)
        return conn

    def close_thread_connection(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            with self._connections_lock:
                if conn in self._all_connections:
                    self._all_connections.remove(conn)
            try:
                conn.close()
            finally:
                self._local.conn = None

    def close(self) -> None:
        """Close every connection created by this process."""
        with self._connections_lock:
            connections, self._all_connections = self._all_connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        self._local = threading.local()

    # -- query helpers -----------------------------------------------------
    def execute(
        self,
        sql: str,
        params: Sequence[Any] | dict[str, Any] | None = None,
    ) -> sqlite3.Cursor:
        try:
            return self.connect().execute(sql, params or ())
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        try:
            return self.connect().executemany(sql, seq)
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc

    def execute_script(self, script: str) -> None:
        try:
            self.connect().executescript(script)
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[Row]:
        cursor = self.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None
    ) -> Row | None:
        cursor = self.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> Any:
        cursor = self.execute(sql, params)
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def insert(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
        """Run an INSERT and return the new ``rowid``."""
        cursor = self.execute(sql, params)
        return int(cursor.lastrowid or 0)

    # -- transactions ------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction.

        A process-level re-entrant lock is taken in addition to SQLite's own
        locking so that two write transactions in the same process queue
        instead of one of them seeing ``SQLITE_BUSY`` mid-statement.  The lock
        is only held for the duration of the ``with`` block.
        """
        conn = self.connect()
        with self._write_lock:
            if conn.in_transaction:
                # Nested use inside an outer transaction: join it.
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def health_check(self) -> dict[str, Any]:
        """Cheap readiness probe used by ``GET /api/health``."""
        started = utc_now_iso()
        try:
            conn = self.connect()
            result = conn.execute("SELECT 1 AS ok").fetchone()
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            expected = {"students", "student_barcodes", "scan_logs", "transactions", "users"}
            return {
                "status": "ok" if result and expected <= tables else "degraded",
                "path": self.path.name,
                "tables": sorted(tables & expected),
                "missing_tables": sorted(expected - tables),
                "checked_at": started,
            }
        except DatabaseError as exc:
            return {
                "status": "error",
                "path": self.path.name,
                "error": str(exc),
                "checked_at": started,
            }

    @staticmethod
    def _translate(exc: sqlite3.Error) -> DatabaseError:
        """Map driver errors to a safe domain error.

        The SQLite message can contain table/column names; it is logged but not
        returned to the client, in line with the "no sensitive database
        information" requirement.
        """
        logger.error("database error: %s", exc)
        if isinstance(exc, sqlite3.IntegrityError):
            return DatabaseError("the operation violates a database constraint")
        if isinstance(exc, sqlite3.OperationalError):
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return DatabaseError("the database is busy, please retry")
            return DatabaseError("the database is not available")
        return DatabaseError("database operation failed")


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
_DATABASE: Database | None = None
_DB_LOCK = threading.Lock()


def get_database(config: AppConfig | None = None) -> Database:
    global _DATABASE
    with _DB_LOCK:
        if _DATABASE is None:
            cfg = config or get_config()
            _DATABASE = Database(
                cfg.database_path,
                busy_timeout_ms=cfg.database.busy_timeout_ms,
                enable_wal=cfg.database.enable_wal,
            )
        return _DATABASE


def set_database(database: Database | None) -> None:
    """Inject a database (tests use a temporary file)."""
    global _DATABASE
    with _DB_LOCK:
        if _DATABASE is not None and _DATABASE is not database:
            _DATABASE.close()
        _DATABASE = database


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Module-level shortcut for ``get_database().transaction()``."""
    with get_database().transaction() as conn:
        yield conn


def init_database(config: AppConfig | None = None) -> Database:
    """Create the schema if the database file does not exist yet."""
    cfg = config or get_config()
    database = get_database(cfg)
    if not database.exists:
        logger.info("creating database at %s", database.path)
        database.execute_script(cfg.schema_path.read_text(encoding="utf-8"))
    return database
