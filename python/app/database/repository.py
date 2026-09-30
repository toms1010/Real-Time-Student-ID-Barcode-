"""Repository layer - **all** SQL in the project lives here (or in
``cpp/src/database/DatabaseClient.cpp`` for the C++ side).

Rules enforced by convention and review:

* Every statement is parameterised.  Identifiers (table/column names) are never
  taken from user input; dynamic fragments are chosen from fixed allow-lists in
  this module.
* Ordering and filtering are expressed with a small set of literal SQL strings
  selected by an enum-ish string key, so an attacker cannot inject through
  ``sort=``.
* No method returns a raw ``sqlite3`` row; everything is converted to
  ``app.database.models`` dataclasses.

The service layer (``app/services``) is the only caller, which keeps the HTTP
and business-logic concerns out of the SQL.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from app.database.connection import Database, get_database
from app.database.models import Barcode, ScanLog, Session, Student, Transaction, User
from app.utils.errors import NotFoundError, ValidationError
from app.utils.logger import get_logger
from app.utils.security import generate_reference_number
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)

ScanResult = Literal[
    "VERIFIED", "UNKNOWN_ID", "INVALID_BARCODE", "DUPLICATE_SCAN", "UNSUPPORTED_FORMAT", "ERROR"
]
StudentSort = Literal["student_id", "name", "created_at", "updated_at", "status", "course"]
ScanSort = Literal["scan_time", "student_id", "result", "processing_time_ms"]
ScanOrder = Literal["asc", "desc"]

_STUDENT_SORT_SQL: dict[str, str] = {
    "student_id": "s.student_id COLLATE NOCASE",
    "name": "s.last_name COLLATE NOCASE, s.first_name COLLATE NOCASE",
    "created_at": "s.created_at",
    "updated_at": "s.updated_at",
    "status": "s.status, s.student_id",
    "course": "s.course COLLATE NOCASE, s.student_id",
}
_SCAN_SORT_SQL: dict[str, str] = {
    "scan_time": "sl.scan_time",
    "student_id": "st.student_id COLLATE NOCASE",
    "result": "sl.result, sl.scan_time",
    "processing_time_ms": "sl.processing_time_ms",
}
_SCAN_RESULT_SQL: dict[str, str] = {
    "VERIFIED": "sl.result = 'VERIFIED'",
    "UNKNOWN_ID": "sl.result = 'UNKNOWN_ID'",
    "INVALID_BARCODE": "sl.result = 'INVALID_BARCODE'",
    "DUPLICATE_SCAN": "sl.result = 'DUPLICATE_SCAN'",
    "UNSUPPORTED_FORMAT": "sl.result = 'UNSUPPORTED_FORMAT'",
    "ERROR": "sl.result = 'ERROR'",
}


def _order_clause(mapping: dict[str, str], key: str | None, order: str) -> str:
    column = mapping.get(key or "", None)
    if column is None:
        column = mapping[next(iter(mapping))]
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return f"ORDER BY {column} {direction}, sl.id DESC" if column.startswith("sl.") else \
        f"ORDER BY {column} {direction}"


class StudentRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    # -- reads -------------------------------------------------------------
    def list(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        course: str | None = None,
        year_level: int | None = None,
        sort: StudentSort = "student_id",
        order: ScanOrder = "asc",
        limit: int = 50,
        offset: int = 0,
        include_barcodes: bool = True,
    ) -> tuple[list[Student], int]:
        where: list[str] = []
        params: list[Any] = []
        if search:
            needle = f"%{search.strip()}%"
            where.append(
                "(s.student_id LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ? "
                "OR s.middle_name LIKE ? OR s.email LIKE ? "
                "OR (s.first_name || ' ' || s.last_name) LIKE ? "
                "OR EXISTS (SELECT 1 FROM student_barcodes b "
                "           WHERE b.student_id = s.id AND b.barcode_value LIKE ?))"
            )
            params.extend([needle] * 7)
        if status:
            where.append("s.status = ?")
            params.append(status)
        if course:
            where.append("s.course = ?")
            params.append(course)
        if year_level is not None:
            where.append("s.year_level = ?")
            params.append(year_level)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.db.scalar(f"SELECT COUNT(*) FROM students s{clause}", params) or 0
        )
        order_sql = _order_clause(_STUDENT_SORT_SQL, sort, order)
        rows = self.db.query(
            f"""
            SELECT s.*,
                   (SELECT GROUP_CONCAT(b.barcode_value, '|')
                      FROM student_barcodes b
                     WHERE b.student_id = s.id AND b.retired_at IS NULL) AS barcode_values
              FROM students s{clause}
              {order_sql}
              LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        )
        students = [Student.from_row(row) for row in rows]  # type: ignore[misc]
        if include_barcodes and students:
            self._attach_barcodes(students)
        return students, total  # type: ignore[return-value]

    def _attach_barcodes(self, students: list[Student]) -> None:
        values = ",".join("?" * len(students))
        rows = self.db.query(
            f"""
            SELECT b.* FROM student_barcodes b
             WHERE b.student_id IN ({values})
             ORDER BY b.is_primary DESC, b.id
            """,
            [s.id for s in students],
        )
        grouped: dict[int, list[Barcode]] = {}
        for row in rows:
            barcode = Barcode.from_row(row)
            if barcode and barcode.student_id is not None:
                grouped.setdefault(barcode.student_id, []).append(barcode)
        for student in students:
            if student.id is not None:
                student.barcodes = grouped.get(student.id, [])

    def get_by_student_id(self, student_id: str) -> Student | None:
        row = self.db.query_one("SELECT * FROM students WHERE student_id = ?", (student_id,))
        student = Student.from_row(row)
        if student:
            self._attach_barcodes([student])
        return student

    def get_by_pk(self, pk: int) -> Student | None:
        row = self.db.query_one("SELECT * FROM students WHERE id = ?", (pk,))
        student = Student.from_row(row)
        if student:
            self._attach_barcodes([student])
        return student

    def get_many_by_student_id(self, student_ids: list[str]) -> list[Student]:
        if not student_ids:
            return []
        values = ",".join("?" * len(student_ids))
        rows = self.db.query(
            f"SELECT * FROM students WHERE student_id IN ({values})", student_ids
        )
        students = [Student.from_row(r) for r in rows]  # type: ignore[misc]
        self._attach_barcodes(students)  # type: ignore[arg-type]
        return students  # type: ignore[return-value]

    def distinct_courses(self) -> list[str]:
        rows = self.db.query(
            "SELECT DISTINCT course FROM students WHERE course IS NOT NULL ORDER BY course"
        )
        return [row["course"] for row in rows]

    def counts_by_status(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM students GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    # -- writes ------------------------------------------------------------
    def create(self, data: dict[str, Any]) -> Student:
        payload = _student_payload(data)
        now = utc_now_iso()
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO students (student_id, first_name, middle_name, last_name, course,
                                          year_level, section, school, email, phone, photo_path,
                                          status, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["student_id"],
                        payload["first_name"],
                        payload["middle_name"],
                        payload["last_name"],
                        payload["course"],
                        payload["year_level"],
                        payload["section"],
                        payload["school"],
                        payload["email"],
                        payload["phone"],
                        payload.get("photo_path"),
                        payload["status"],
                        payload["notes"],
                        now,
                        now,
                    ),
                )
                pk = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError as exc:
            if "student_id" in str(exc):
                raise ValidationError(
                    f"student_id {payload['student_id']!r} already exists"
                ) from exc
            raise
        student = self.get_by_pk(pk)
        assert student is not None
        return student

    def update(self, pk: int, data: dict[str, Any]) -> Student:
        existing = self.get_by_pk(pk)
        if existing is None:
            raise NotFoundError(f"student {pk} not found")
        merged = existing.as_dict()
        merged.pop("barcodes", None)
        merged.update({k: v for k, v in data.items() if v is not None or k in data})
        payload = _student_payload(merged)
        self.db.execute(
            """
            UPDATE students
               SET student_id = ?, first_name = ?, middle_name = ?, last_name = ?,
                   course = ?, year_level = ?, section = ?, school = ?, email = ?,
                   phone = ?, photo_path = ?, status = ?, notes = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                payload["student_id"],
                payload["first_name"],
                payload["middle_name"],
                payload["last_name"],
                payload["course"],
                payload["year_level"],
                payload["section"],
                payload["school"],
                payload["email"],
                payload["phone"],
                payload.get("photo_path"),
                payload["status"],
                payload["notes"],
                utc_now_iso(),
                pk,
            ),
        )
        student = self.get_by_pk(pk)
        assert student is not None
        return student

    def delete(self, pk: int, *, hard: bool = False) -> None:
        """Delete or deactivate a student.

        ``hard=False`` (the default) flips the status to ``inactive`` and
        deactivates the user's accounts, keeping the audit trail intact.  Only an
        administrator may pass ``hard=True``, and only when the record has no
        scan history.
        """
        if hard:
            self.db.execute("DELETE FROM students WHERE id = ?", (pk,))
            return
        self.db.execute(
            "UPDATE students SET status = 'inactive', updated_at = ? WHERE id = ?",
            (utc_now_iso(), pk),
        )
        self.db.execute(
            "UPDATE student_barcodes SET retired_at = ? WHERE student_id = ? AND retired_at IS NULL",
            (utc_now_iso(), pk),
        )

    def reactivate(self, pk: int) -> Student:
        self.db.execute(
            "UPDATE students SET status = 'active', updated_at = ? WHERE id = ?",
            (utc_now_iso(), pk),
        )
        self.db.execute(
            "UPDATE student_barcodes SET retired_at = NULL WHERE student_id = ?", (pk,)
        )
        student = self.get_by_pk(pk)
        if student is None:
            raise NotFoundError(f"student {pk} not found")
        return student

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM students") or 0)


def _student_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a student dict into exact column values."""
    status = str(data.get("status") or "active")
    if status not in {"active", "inactive", "graduated", "suspended", "transferred"}:
        raise ValidationError(f"invalid student status: {status}")
    year = data.get("year_level")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError) as exc:
            raise ValidationError("year_level must be an integer") from exc
        if not 1 <= year <= 12:
            raise ValidationError("year_level must be between 1 and 12")
    return {
        "student_id": str(data["student_id"]).strip(),
        "first_name": str(data["first_name"]).strip(),
        "middle_name": (str(data["middle_name"]).strip() if data.get("middle_name") else None),
        "last_name": str(data["last_name"]).strip(),
        "course": (str(data["course"]).strip() if data.get("course") else None),
        "year_level": year,
        "section": (str(data["section"]).strip() if data.get("section") else None),
        "school": (str(data["school"]).strip() if data.get("school") else None),
        "email": (str(data["email"]).strip() if data.get("email") else None),
        "phone": (str(data["phone"]).strip() if data.get("phone") else None),
        "photo_path": (str(data["photo_path"]) if data.get("photo_path") else None),
        "status": status,
        "notes": (str(data["notes"]).strip() if data.get("notes") else None),
    }


class BarcodeRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def find_by_value(self, value: str) -> Barcode | None:
        row = self.db.query_one(
            "SELECT * FROM student_barcodes WHERE barcode_value = ? AND retired_at IS NULL",
            (value,),
        )
        return Barcode.from_row(row)

    def list_for_student(self, pk: int) -> list[Barcode]:
        rows = self.db.query(
            "SELECT * FROM student_barcodes WHERE student_id = ? ORDER BY is_primary DESC, id",
            (pk,),
        )
        return [Barcode.from_row(r) for r in rows]  # type: ignore[misc]

    def add(
        self,
        student_pk: int,
        value: str,
        *,
        barcode_type: str = "Code128",
        label: str | None = None,
        is_primary: bool = False,
    ) -> Barcode:
        try:
            with self.db.transaction() as conn:
                if is_primary:
                    conn.execute(
                        "UPDATE student_barcodes SET is_primary = 0 WHERE student_id = ?",
                        (student_pk,),
                    )
                cursor = conn.execute(
                    """
                    INSERT INTO student_barcodes
                        (student_id, barcode_value, barcode_type, label, is_primary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (student_pk, value, barcode_type, label, 1 if is_primary else 0, utc_now_iso()),
                )
                pk = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"barcode {value!r} is already registered") from exc
        return Barcode(id=pk, student_id=student_pk, barcode_value=value,
                       barcode_type=barcode_type, label=label, is_primary=is_primary)

    def remove(self, barcode_pk: int) -> None:
        self.db.execute("DELETE FROM student_barcodes WHERE id = ?", (barcode_pk,))

    def retire(self, barcode_pk: int) -> None:
        self.db.execute(
            "UPDATE student_barcodes SET retired_at = ? WHERE id = ? AND retired_at IS NULL",
            (utc_now_iso(), barcode_pk),
        )


class ScanRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def record(
        self,
        *,
        barcode_value: str,
        result: ScanResult,
        student_pk: int | None = None,
        confidence: float | None = None,
        barcode_type: str | None = None,
        device_name: str | None = None,
        source: str = "cpp",
        processing_time_ms: float | None = None,
        detection_time_ms: float | None = None,
        database_time_ms: float | None = None,
        frame_path: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        operator_id: int | None = None,
        scan_time: str | None = None,
    ) -> int:
        """Insert one scan log row and return its id."""
        return self.db.insert(
            """
            INSERT INTO scan_logs
                (student_id, barcode_value, scan_time, result, confidence, barcode_type,
                 device_name, source, processing_time_ms, detection_time_ms, database_time_ms,
                 frame_path, error_code, error_message, operator_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_pk,
                barcode_value,
                scan_time or utc_now_iso(),
                result,
                confidence,
                barcode_type,
                device_name,
                source,
                processing_time_ms,
                detection_time_ms,
                database_time_ms,
                frame_path,
                error_code,
                error_message,
                operator_id,
            ),
        )

    _SELECT = """
        SELECT sl.*,
               st.student_id AS student_number,
               st.first_name AS first_name,
               st.middle_name AS middle_name,
               st.last_name AS last_name,
               st.course AS course,
               st.year_level AS year_level,
               st.section AS section,
               st.status AS status
          FROM scan_logs sl
          LEFT JOIN students st ON st.id = sl.student_id
    """

    def get(self, scan_pk: int) -> ScanLog | None:
        return ScanLog.from_row(self.db.query_one(f"{self._SELECT} WHERE sl.id = ?", (scan_pk,)))

    def list(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        result: str | None = None,
        search: str | None = None,
        student_id: str | None = None,
        device_name: str | None = None,
        sort: ScanSort = "scan_time",
        order: ScanOrder = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ScanLog], int]:
        where: list[str] = []
        params: list[Any] = []
        if start:
            where.append("sl.scan_time >= ?")
            params.append(start)
        if end:
            where.append("sl.scan_time < ?")
            params.append(end)
        if result:
            clause = _SCAN_RESULT_SQL.get(result)
            if clause is None:
                raise ValidationError(f"unknown result filter: {result}")
            where.append(clause)
        if student_id:
            where.append("st.student_id = ?")
            params.append(student_id)
        if device_name:
            where.append("sl.device_name = ?")
            params.append(device_name)
        if search:
            needle = f"%{search.strip()}%"
            where.append(
                "(sl.barcode_value LIKE ? OR st.student_id LIKE ? "
                "OR (st.first_name || ' ' || st.last_name) LIKE ?)"
            )
            params.extend([needle] * 3)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.db.scalar(
                f"SELECT COUNT(*) FROM scan_logs sl LEFT JOIN students st ON st.id = sl.student_id{clause}",
                params,
            )
            or 0
        )
        order_sql = _order_clause(_SCAN_SORT_SQL, sort, order)
        rows = self.db.query(
            f"{self._SELECT}{clause} {order_sql} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return [ScanLog.from_row(r) for r in rows], total  # type: ignore[misc,return-value]

    def recent(self, limit: int = 10) -> list[ScanLog]:
        rows = self.db.query(
            f"{self._SELECT} ORDER BY sl.scan_time DESC, sl.id DESC LIMIT ?", (int(limit),)
        )
        return [ScanLog.from_row(r) for r in rows]  # type: ignore[misc]

    def summary(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        where, params = _range(start, end, "sl.scan_time")
        row = self.db.query_one(
            f"""
            SELECT COUNT(*)                                              AS total,
                   COALESCE(SUM(sl.result = 'VERIFIED'), 0)              AS verified,
                   COALESCE(SUM(sl.result = 'UNKNOWN_ID'), 0)            AS unknown_id,
                   COALESCE(SUM(sl.result = 'INVALID_BARCODE'), 0)       AS invalid_barcode,
                   COALESCE(SUM(sl.result = 'DUPLICATE_SCAN'), 0)        AS duplicate_scan,
                   COALESCE(SUM(sl.result = 'UNSUPPORTED_FORMAT'), 0)    AS unsupported_format,
                   COALESCE(SUM(sl.result = 'ERROR'), 0)                 AS errors,
                   ROUND(AVG(sl.processing_time_ms), 2)                 AS avg_processing_ms,
                   ROUND(MAX(sl.processing_time_ms), 2)                 AS max_processing_ms,
                   ROUND(AVG(sl.confidence), 4)                         AS avg_confidence
              FROM scan_logs sl{where}
            """,
            params,
        )
        return row or {}

    def daily(
        self, start: str | None = None, end: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Daily roll-up from ``v_daily_scan_summary`` (newest first)."""
        where: list[str] = []
        params: list[Any] = []
        if start:
            where.append("scan_date >= ?")
            params.append(start[:10])
        if end:
            where.append("scan_date <= ?")
            params.append(end[:10])
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return self.db.query(
            f"SELECT * FROM v_daily_scan_summary{clause} ORDER BY scan_date DESC LIMIT ?",
            [*params, int(limit)],
        )

    def unknown_barcodes(
        self, start: str | None = None, end: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where, params = _range(start, end, "sl.scan_time")
        # `_range` already emits the WHERE keyword when a bound is present.
        condition = where[len(" WHERE"):] if where else ""
        joined = (
            f" AND {condition}" if condition else ""
        )
        return self.db.query(
            f"""
            SELECT sl.barcode_value,
                   COUNT(*)                AS attempts,
                   MAX(sl.scan_time)       AS last_seen,
                   MIN(sl.scan_time)       AS first_seen
              FROM scan_logs sl
             WHERE sl.result IN ('UNKNOWN_ID', 'INVALID_BARCODE', 'UNSUPPORTED_FORMAT'){joined}
             GROUP BY sl.barcode_value
             ORDER BY attempts DESC, last_seen DESC
             LIMIT ?
            """,
            [*params, int(limit)],
        )

    def hourly_profile(self, start: str, end: str) -> list[dict[str, Any]]:
        """Scans per hour of day - used to size the cashier peak windows."""
        return self.db.query(
            """
            SELECT substr(scan_time, 12, 2) AS hour,
                   COUNT(*)                  AS scans,
                   COALESCE(SUM(result = 'VERIFIED'), 0) AS verified
              FROM scan_logs
             WHERE scan_time >= ? AND scan_time < ?
             GROUP BY hour
             ORDER BY hour
            """,
            (start, end),
        )

    def top_students(self, start: str, end: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT student_number, full_name, course, total_scans, last_scan_time
              FROM v_student_scan_history
             WHERE total_scans > 0 AND last_scan_time >= ? AND last_scan_time < ?
             ORDER BY total_scans DESC, student_number
             LIMIT ?
            """,
            (start, end, int(limit)),
        )

    def slowest(self, start: str, end: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT sl.id, sl.scan_time, sl.barcode_value, sl.result,
                   sl.processing_time_ms, sl.detection_time_ms, sl.database_time_ms,
                   sl.device_name
              FROM scan_logs sl
             WHERE sl.scan_time >= ? AND sl.scan_time < ?
               AND sl.result = 'VERIFIED'
             ORDER BY sl.processing_time_ms DESC
             LIMIT ?
            """,
            (start, end, int(limit)),
        )


def _range(start: str | None, end: str | None, column: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if start:
        clauses.append(f"{column} >= ?")
        params.append(start)
    if end:
        clauses.append(f"{column} < ?")
        params.append(end)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


class TransactionRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    _SELECT = """
        SELECT t.*,
               st.student_id AS student_number,
               st.first_name AS first_name,
               st.middle_name AS middle_name,
               st.last_name AS last_name,
               st.course AS course,
               st.year_level AS year_level,
               st.section AS section,
               st.status AS status,
               u.username AS cashier_username
          FROM transactions t
          LEFT JOIN students st ON st.id = t.student_id
          LEFT JOIN users u ON u.id = t.cashier_id
    """

    def create(
        self,
        *,
        student_pk: int | None,
        transaction_type: str,
        amount: float,
        cashier_id: int | None,
        scan_id: int | None = None,
        quantity: int = 1,
        reference_note: str | None = None,
        status: str = "completed",
    ) -> Transaction:
        reference = generate_reference_number()
        # Retry on the (astronomically unlikely) reference collision.
        for _ in range(3):
            try:
                pk = self.db.insert(
                    """
                    INSERT INTO transactions
                        (reference_number, student_id, scan_id, transaction_type, amount,
                         quantity, cashier_id, status, reference_note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference, student_pk, scan_id, transaction_type, float(amount),
                        int(quantity), cashier_id, status, reference_note,
                        utc_now_iso(), utc_now_iso(),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                reference = generate_reference_number()
        else:  # pragma: no cover
            raise ValidationError("could not allocate a transaction reference number")
        return self.get(pk)  # type: ignore[return-value]

    def get(self, pk: int) -> Transaction | None:
        return Transaction.from_row(self.db.query_one(f"{self._SELECT} WHERE t.id = ?", (pk,)))

    def get_by_reference(self, reference: str) -> Transaction | None:
        return Transaction.from_row(
            self.db.query_one(f"{self._SELECT} WHERE t.reference_number = ?", (reference,))
        )

    def list(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        transaction_type: str | None = None,
        status: str | None = None,
        student_id: str | None = None,
        cashier_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Transaction], int]:
        where: list[str] = []
        params: list[Any] = []
        if start:
            where.append("t.created_at >= ?")
            params.append(start)
        if end:
            where.append("t.created_at < ?")
            params.append(end)
        if transaction_type:
            where.append("t.transaction_type = ?")
            params.append(transaction_type)
        if status:
            where.append("t.status = ?")
            params.append(status)
        if student_id:
            where.append("st.student_id = ?")
            params.append(student_id)
        if cashier_id is not None:
            where.append("t.cashier_id = ?")
            params.append(cashier_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.db.scalar(
                f"SELECT COUNT(*) FROM transactions t LEFT JOIN students st ON st.id = t.student_id{clause}",
                params,
            )
            or 0
        )
        rows = self.db.query(
            f"{self._SELECT}{clause} ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return [Transaction.from_row(r) for r in rows], total  # type: ignore[misc,return-value]

    def set_status(self, pk: int, status: str) -> Transaction:
        if status not in {"pending", "completed", "cancelled", "refunded"}:
            raise ValidationError(f"invalid transaction status: {status}")
        self.db.execute(
            "UPDATE transactions SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now_iso(), pk),
        )
        txn = self.get(pk)
        if txn is None:
            raise NotFoundError(f"transaction {pk} not found")
        return txn

    def totals(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        clause, params = _range(start, end, "t.created_at")
        return self.db.query_one(
            f"""
            SELECT COUNT(*)                                              AS count,
                   COALESCE(SUM(t.amount), 0)                            AS total_amount,
                   COALESCE(SUM(t.amount), 0) / NULLIF(COUNT(*), 0)      AS avg_amount,
                   COALESCE(SUM(t.status = 'completed'), 0)              AS completed,
                   COALESCE(SUM(t.status = 'cancelled'), 0)              AS cancelled
              FROM transactions t{clause}
            """,
            params,
        ) or {}


class UserRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def get_by_username(self, username: str) -> User | None:
        return User.from_row(self.db.query_one("SELECT * FROM users WHERE username = ?", (username,)))

    def get(self, pk: int) -> User | None:
        return User.from_row(self.db.query_one("SELECT * FROM users WHERE id = ?", (pk,)))

    def list(self) -> list[User]:
        return [User.from_row(r) for r in self.db.query("SELECT * FROM users ORDER BY id")]  # type: ignore[misc]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users") or 0)

    def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "operator",
        full_name: str | None = None,
        must_change_password: bool = False,
    ) -> User:
        if role not in {"admin", "cashier", "staff", "operator", "viewer"}:
            raise ValidationError(f"invalid role: {role}")
        now = utc_now_iso()
        try:
            pk = self.db.insert(
                """
                INSERT INTO users (username, password_hash, full_name, role, is_active,
                                   must_change_password, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (username, password_hash, full_name, role, 1 if must_change_password else 0,
                 now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"username {username!r} already exists") from exc
        user = self.get(pk)
        assert user is not None
        return user

    def set_password(self, user_pk: int, password_hash: str, *, must_change: bool = False) -> None:
        self.db.execute(
            """
            UPDATE users
               SET password_hash = ?, must_change_password = ?, updated_at = ?
             WHERE id = ?
            """,
            (password_hash, 1 if must_change else 0, utc_now_iso(), user_pk),
        )

    def set_role(self, user_pk: int, role: str) -> None:
        if role not in {"admin", "cashier", "staff", "operator", "viewer"}:
            raise ValidationError(f"invalid role: {role}")
        self.db.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, utc_now_iso(), user_pk)
        )

    def set_active(self, user_pk: int, active: bool) -> None:
        self.db.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, utc_now_iso(), user_pk),
        )

    def touch_login(self, user_pk: int) -> None:
        self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utc_now_iso(), user_pk))

    # -- sessions ----------------------------------------------------------
    def create_session(
        self,
        *,
        user_pk: int,
        token_hash: str,
        expires_at: str,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        self.db.insert(
            """
            INSERT INTO user_sessions (user_id, token_hash, expires_at, user_agent, client_ip)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_pk, token_hash, expires_at, user_agent, client_ip),
        )

    def count_sessions(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM user_sessions") or 0)

    def find_session(self, token_hash: str) -> tuple[Session, User] | None:
        row = self.db.query_one(
            """
            SELECT s.id              AS session_id,
                   s.user_id         AS user_pk,
                   s.token_hash      AS token_hash,
                   s.created_at      AS created_at,
                   s.expires_at      AS expires_at,
                   s.revoked_at      AS revoked_at,
                   s.user_agent      AS user_agent,
                   s.client_ip       AS client_ip,
                   u.id              AS id,
                   u.username        AS username,
                   u.password_hash   AS password_hash,
                   u.full_name       AS full_name,
                   u.role            AS role,
                   u.is_active       AS is_active,
                   u.must_change_password AS must_change_password,
                   u.created_at      AS user_created_at,
                   u.updated_at      AS user_updated_at,
                   u.last_login_at   AS last_login_at
              FROM user_sessions s
              JOIN users u ON u.id = s.user_id
             WHERE s.token_hash = ? AND s.revoked_at IS NULL
            """,
            (token_hash,),
        )
        if not row:
            return None
        session = Session(
            id=row["session_id"],
            user_id=row["user_pk"],
            token_hash=row["token_hash"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            user_agent=row["user_agent"],
            client_ip=row["client_ip"],
        )
        user = User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            full_name=row["full_name"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=row["user_created_at"],
            updated_at=row["user_updated_at"],
            last_login_at=row["last_login_at"],
        )
        return session, user

    def revoke_session(self, token_hash: str) -> None:
        self.db.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now_iso(), token_hash),
        )

    def revoke_all_sessions(self, user_pk: int) -> None:
        self.db.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (utc_now_iso(), user_pk),
        )

    def purge_expired_sessions(self) -> int:
        return self.db.execute(
            "DELETE FROM user_sessions WHERE expires_at < ?", (utc_now_iso(),)
        ).rowcount

    # -- login throttling --------------------------------------------------
    def record_attempt(self, username: str, ip: str | None, successful: bool) -> None:
        self.db.insert(
            "INSERT INTO login_attempts (username, ip_address, successful) VALUES (?, ?, ?)",
            (username, ip, 1 if successful else 0),
        )

    def recent_failures(self, username: str, since_iso: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE username = ? AND successful = 0 AND attempted_at >= ?",
                (username, since_iso),
            )
            or 0
        )

    def clear_failures(self, username: str) -> None:
        self.db.execute("DELETE FROM login_attempts WHERE username = ?", (username,))


class AuditRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def log(
        self,
        *,
        action: str,
        user_id: int | None = None,
        username: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Append an audit row.

        Audit logging is *best effort*: if the write fails (a full disk, an
        unexpected constraint) the business operation must still succeed, so the
        error is logged and swallowed here rather than propagating as a 500.
        """
        import json

        try:
            self.db.insert(
                """
                INSERT INTO audit_logs (user_id, username, action, entity_type, entity_id,
                                        details, ip_address)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, username, action, entity_type, entity_id,
                    json.dumps(details or {}, default=str), ip_address,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("audit log write failed for action=%s: %s", action, exc)

    def list(
        self,
        *,
        action: str | None = None,
        user_id: int | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if action:
            where.append("action LIKE ?")
            params.append(f"%{action}%")
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if start:
            where.append("created_at >= ?")
            params.append(start)
        if end:
            where.append("created_at < ?")
            params.append(end)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM audit_logs{clause}", params) or 0)
        rows = self.db.query(
            f"SELECT * FROM audit_logs{clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return rows, total

    def purge_older_than(self, cutoff_iso: str) -> int:
        return self.db.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff_iso,)).rowcount


class SettingsRepository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def all(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.db.query("SELECT key, value FROM system_settings")}

    def typed(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM system_settings ORDER BY category, key")

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM system_settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str, *, user_id: int | None = None) -> None:
        self.db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                            updated_at = excluded.updated_at,
                                            updated_by = excluded.updated_by
            """,
            (key, value, utc_now_iso(), user_id),
        )


class Repository:
    """Facade grouping the repositories (injected into the services)."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()
        self.students = StudentRepository(self.db)
        self.barcodes = BarcodeRepository(self.db)
        self.scans = ScanRepository(self.db)
        self.transactions = TransactionRepository(self.db)
        self.users = UserRepository(self.db)
        self.audit = AuditRepository(self.db)
        self.settings = SettingsRepository(self.db)
