"""Reporting and analytics.

The report service never invents numbers: every figure is a SQL aggregate over
``scan_logs`` / ``transactions`` / ``students``.  Anything that requires a
measurement (accuracy, average FPS) is either computed from recorded scan rows
or reported as ``None`` so the documentation can show ``[MEASURE]`` rather than
a fabricated figure (ADR-010).
"""

from __future__ import annotations

import math
from typing import Any

from app.database.repository import Repository
from app.schemas.report import (
    DailyReport,
    DashboardSummary,
    PerformanceReport,
    ReportRequest,
)
from app.utils.config import AppConfig, get_config
from app.utils.logger import get_logger
from app.utils.timeutil import format_iso, to_local, utc_now, utc_now_iso

logger = get_logger(__name__)


class ReportService:
    def __init__(self, repo: Repository | None = None, config: AppConfig | None = None) -> None:
        self.repo = repo or Repository()
        self.config = config or get_config()

    @property
    def timezone(self) -> str:
        return self.config.reports.timezone

    # -- daily / weekly / monthly -----------------------------------------
    def daily(self, request: ReportRequest) -> DailyReport:
        start, end, label = request.resolve_range(self.timezone)
        totals = self.repo.scans.summary(start, end)
        days = self.repo.scans.daily(start, end, limit=400)
        return DailyReport(
            period=request.period,
            label=label,
            start=start,
            end=end,
            timezone=self.timezone,
            totals=totals,
            days=days,
            top_students=self.repo.scans.top_students(start, end, limit=10),
            unknown_barcodes=self.repo.scans.unknown_barcodes(start, end, limit=25),
            hourly_profile=self.repo.scans.hourly_profile(start, end),
            generated_at=utc_now_iso(),
        )

    # -- dashboard ---------------------------------------------------------
    def summary(self) -> DashboardSummary:
        today = self._today_bounds()
        status_counts = self.repo.students.counts_by_status()
        today_scans = self.repo.scans.summary(*today)
        txn_totals = self.repo.transactions.totals(*today)
        last = self.repo.scans.recent(limit=1)
        return DashboardSummary(
            students_total=sum(status_counts.values()),
            students_active=status_counts.get("active", 0),
            students_inactive=status_counts.get("inactive", 0),
            scans_total=int(self.repo.db.scalar("SELECT COUNT(*) FROM scan_logs") or 0),
            scans_today=int(today_scans.get("total") or 0),
            verified_today=int(today_scans.get("verified") or 0),
            errors_today=int(today_scans.get("errors") or 0),
            unknown_today=int(today_scans.get("unknown_id") or 0),
            duplicates_today=int(today_scans.get("duplicate_scan") or 0),
            transactions_today=int(txn_totals.get("count") or 0),
            revenue_today=round(float(txn_totals.get("total_amount") or 0.0), 2),
            avg_processing_ms=today_scans.get("avg_processing_ms"),
            last_scan=(
                {
                    "id": last[0].id,
                    "student_id": last[0].student_number,
                    "name": last[0].student.full_name if last[0].student else None,
                    "barcode_value": last[0].barcode_value,
                    "result": last[0].result,
                    "scan_time": last[0].scan_time,
                }
                if last
                else None
            ),
            trend_7d=self.repo.scans.daily(limit=7),
            hourly_today=self.repo.scans.hourly_profile(*today),
            generated_at=utc_now_iso(),
        )

    # -- performance -------------------------------------------------------
    def performance(self, request: ReportRequest) -> PerformanceReport:
        start, end, label = request.resolve_range(self.timezone)
        rows = self.repo.db.query(
            """
            SELECT sl.processing_time_ms, sl.detection_time_ms, sl.database_time_ms,
                   sl.confidence, sl.result, sl.device_name, sl.barcode_value, sl.scan_time,
                   sl.id
              FROM scan_logs sl
             WHERE sl.scan_time >= ? AND sl.scan_time < ?
            """,
            (start, end),
        )
        processing = [r["processing_time_ms"] for r in rows if r["processing_time_ms"] is not None]
        detection = [r["detection_time_ms"] for r in rows if r["detection_time_ms"] is not None]
        database = [r["database_time_ms"] for r in rows if r["database_time_ms"] is not None]
        confidences = [r["confidence"] for r in rows if r["confidence"] is not None]
        verified = sum(1 for r in rows if r["result"] == "VERIFIED")

        by_device: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row["device_name"] or "unknown"
            bucket = by_device.setdefault(
                key, {"device_name": key, "scans": 0, "verified": 0,
                      "avg_processing_ms": [], "avg_confidence": []}
            )
            bucket["scans"] += 1
            bucket["verified"] += 1 if row["result"] == "VERIFIED" else 0
            if row["processing_time_ms"] is not None:
                bucket["avg_processing_ms"].append(row["processing_time_ms"])
            if row["confidence"] is not None:
                bucket["avg_confidence"].append(row["confidence"])
        device_rows = [
            {
                "device_name": bucket["device_name"],
                "scans": bucket["scans"],
                "verified": bucket["verified"],
                "avg_processing_ms": _mean(bucket["avg_processing_ms"]),
                "avg_confidence": _round(_mean(bucket["avg_confidence"]), 4),
            }
            for bucket in by_device.values()
        ]
        device_rows.sort(key=lambda r: r["scans"], reverse=True)

        return PerformanceReport(
            period=request.period,
            label=label,
            start=start,
            end=end,
            frames_observed=len(rows),
            scans=len(rows),
            verified=verified,
            failed=len(rows) - verified,
            avg_processing_ms=_round(_mean(processing), 3),
            min_processing_ms=_round(min(processing) if processing else None, 3),
            max_processing_ms=_round(max(processing) if processing else None, 3),
            avg_detection_ms=_round(_mean(detection), 3),
            avg_database_ms=_round(_mean(database), 3),
            p95_processing_ms=_round(_percentile(processing, 0.95), 3),
            avg_confidence=_round(_mean(confidences), 4),
            slowest=self.repo.scans.slowest(start, end, limit=10),
            by_device=device_rows,
            generated_at=utc_now_iso(),
        )

    # -- student history ---------------------------------------------------
    def student_history(self, student_number: str) -> dict[str, Any]:
        student = self.repo.students.get_by_student_id(student_number)
        if student is None:
            return {"student": None, "scans": [], "totals": {}}
        scans, total = self.repo.scans.list(student_id=student_number, limit=200)
        transactions, _ = self.repo.transactions.list(student_id=student_number, limit=200)
        return {
            "student": {
                "student_id": student.student_id,
                "full_name": student.full_name,
                "course": student.course,
                "year_level": student.year_level,
                "section": student.section,
                "status": student.status,
            },
            "scans": [s.as_dict() for s in scans],
            "transactions": [t.as_dict() for t in transactions],
            "totals": self.repo.scans.summary(),
            "scan_count": total,
        }

    # -- unknown barcode report -------------------------------------------
    def unknown_barcodes(self, request: ReportRequest) -> dict[str, Any]:
        start, end, label = request.resolve_range(self.timezone)
        return {
            "label": label,
            "start": start,
            "end": end,
            "rows": self.repo.scans.unknown_barcodes(start, end, limit=500),
            "generated_at": utc_now_iso(),
        }

    # -- helpers -----------------------------------------------------------
    def _today_bounds(self) -> tuple[str, str]:
        """Today in the report timezone, returned as UTC instants."""
        from datetime import timedelta

        from app.utils.timeutil import parse_iso, to_local

        local_now = to_local(utc_now(), self.timezone) or utc_now()
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return (
            format_iso(start_local),  # type: ignore[arg-type]
            format_iso(end_local),  # type: ignore[arg-type]
        )

    def ranges(self) -> dict[str, tuple[str, str, str]]:
        """Pre-computed period ranges used by the UI buttons."""
        return {
            "today": (*self._today_bounds(), "Today"),
            "week": (*self._period(timedelta_days=7), "Last 7 days"),
            "month": (*self._period(timedelta_days=30), "Last 30 days"),
        }

    def _period(self, *, timedelta_days: int) -> tuple[str, str]:
        from datetime import timedelta

        end = utc_now()
        return format_iso(end - timedelta(days=timedelta_days)), format_iso(end)  # type: ignore[return-value]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None or math.isnan(value) else round(value, digits)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


_report_service: ReportService | None = None


def get_report_service() -> ReportService:
    global _report_service
    if _report_service is None:
        _report_service = ReportService()
    return _report_service


__all__ = ["ReportService", "get_report_service"]
