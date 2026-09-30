"""Report, dashboard and export routes."""

from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.dependencies import current_user, get_reports, get_repo, get_students
from app.reports.exporters import (
    DAILY_COLUMNS,
    PERFORMANCE_COLUMNS,
    SCAN_COLUMNS,
    STUDENT_HISTORY_COLUMNS,
    TRANSACTION_COLUMNS,
    UNKNOWN_COLUMNS,
    get_exporter,
)
from app.schemas.report import (
    DailyReport,
    DashboardSummary,
    ExportDescriptor,
    ExportRequest,
    PerformanceReport,
    ReportRequest,
)
from app.services.auth_service import AuthenticatedUser
from app.services.report_service import ReportService
from app.services.student_service import StudentService
from app.utils.config import AppConfig, get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "json": "application/json",
}


@router.get("/summary", response_model=DashboardSummary, summary="Dashboard counters")
def summary(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
) -> DashboardSummary:
    return reports.summary()


@router.get("/daily", response_model=DailyReport, summary="Daily / weekly / monthly report")
def daily(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
    period: str = Query(default="daily", pattern="^(daily|weekly|monthly|custom|all)$"),
    date: str | None = Query(default=None, description="Anchor date, YYYY-MM-DD"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
) -> DailyReport:
    return reports.daily(ReportRequest(period=period, date=date, start=start,  # type: ignore[arg-type]
                                       end=end, timezone=timezone))


@router.get("/performance", response_model=PerformanceReport,
            summary="System performance report (real measurements only)")
def performance(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
    period: str = Query(default="daily", pattern="^(daily|weekly|monthly|custom|all)$"),
    date: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> PerformanceReport:
    return reports.performance(ReportRequest(period=period, date=date,  # type: ignore[arg-type]
                                             start=start, end=end))


@router.get("/unknown-barcodes", summary="Repeated unknown / invalid barcodes")
def unknown_barcodes(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
    period: str = Query(default="weekly", pattern="^(daily|weekly|monthly|custom|all)$"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, Any]:
    return reports.unknown_barcodes(
        ReportRequest(period=period, start=start, end=end)  # type: ignore[arg-type]
    )


@router.get("/students/{student_id}", summary="One student's scan and transaction history")
def student_history(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
) -> dict[str, Any]:
    return reports.student_history(student_id)


@router.post("/export", response_model=ExportDescriptor, summary="Export a report")
def export_report(
    payload: ExportRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    reports: Annotated[ReportService, Depends(get_reports)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> ExportDescriptor:
    """Write the export to ``reports.output_dir`` and return a download handle.

    Two-step (create then GET ``/api/reports/export/{filename}``) rather than
    streaming the bytes, so a large export can be generated without holding the
    request open and the UI can show progress and retry.
    """
    user.require("reports:export")
    repo = get_repo()
    rows, columns, title, metadata = _collect_rows(payload, repo)

    path = get_exporter().export(
        rows,
        columns=columns,
        filename=f"{payload.report}",
        fmt=payload.format,
        title=title,
        metadata=metadata,
    )
    return ExportDescriptor(
        filename=path.name,
        format=payload.format,  # type: ignore[arg-type]
        rows=len(rows),
        size_bytes=path.stat().st_size,
        download_url=f"/api/reports/export/{path.name}",
    )


@router.get("/export/{filename}", summary="Download a previously generated export")
def download_export(
    filename: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> FileResponse:
    from app.utils.errors import NotFoundError
    from app.utils.security import ensure_within, safe_filename

    safe = safe_filename(filename, fallback="report")
    path = config.report_dir / safe
    if not ensure_within(config.report_dir, path) or not path.is_file():
        raise NotFoundError("that export does not exist")
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lstrip("."), "application/octet-stream"),
        filename=path.name,
    )


@router.get("/export/{filename}/preview", summary="Preview an export as CSV text")
def preview_export(
    filename: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> StreamingResponse:
    from app.utils.errors import NotFoundError
    from app.utils.security import ensure_within, safe_filename

    safe = safe_filename(filename, fallback="report")
    path = config.report_dir / safe
    if not ensure_within(config.report_dir, path) or not path.is_file():
        raise NotFoundError("that export does not exist")
    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type=_MEDIA_TYPES.get(path.suffix.lstrip("."), "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


def _collect_rows(
    payload: ExportRequest, repo: Any
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], str, dict[str, Any]]:
    """Gather rows + column order for the requested report."""
    metadata = {"range": f"{payload.start or 'beginning'} .. {payload.end or 'now'}"}
    if payload.report == "scans":
        rows, total = repo.scans.list(
            start=payload.start, end=payload.end, result=payload.result,
            student_id=payload.student_id, limit=payload.limit,
        )
        metadata["total_matching"] = total
        return [_scan_row(r) for r in rows], SCAN_COLUMNS, "Scan History", metadata

    if payload.report == "transactions":
        rows, total = repo.transactions.list(
            start=payload.start, end=payload.end, student_id=payload.student_id,
            limit=payload.limit,
        )
        metadata["total_matching"] = total
        return [_txn_row(r) for r in rows], TRANSACTION_COLUMNS, "Transactions", metadata

    if payload.report == "daily":
        rows = repo.scans.daily(payload.start, payload.end, limit=400)
        return list(rows), DAILY_COLUMNS, "Daily Scan Summary", metadata

    if payload.report == "unknown_barcodes":
        rows = repo.scans.unknown_barcodes(payload.start, payload.end, limit=payload.limit)
        return list(rows), UNKNOWN_COLUMNS, "Unknown / Invalid Barcodes", metadata

    if payload.report == "performance":
        from app.services.report_service import ReportService

        report = ReportService(repo=repo).performance(
            ReportRequest(period="custom", start=payload.start or "1970-01-01T00:00:00.000Z",
                          end=payload.end)  # type: ignore[arg-type]
        )
        return list(report.by_device), PERFORMANCE_COLUMNS, "System Performance", metadata

    if payload.report == "student_history":
        student = repo.students.get_by_student_id(payload.student_id or "")
        if student is None:
            from app.utils.errors import NotFoundError

            raise NotFoundError("student not found")
        rows, _ = repo.scans.list(student_id=student.student_id, limit=payload.limit)
        return (
            [{"scan_time": r.scan_time, "barcode_value": r.barcode_value, "result": r.result,
              "confidence": r.confidence, "processing_time_ms": r.processing_time_ms,
              "device_name": r.device_name} for r in rows],
            STUDENT_HISTORY_COLUMNS,
            f"Scan History - {student.full_name}",
            metadata,
        )

    raise ValueError(f"unsupported report: {payload.report}")


def _scan_row(row: Any) -> dict[str, Any]:
    student = row.student
    return {
        "scan_time": row.scan_time,
        "student_number": getattr(row, "student_number", None),
        "student_name": student.full_name if student else None,
        "course": getattr(student, "course", None),
        "barcode_value": row.barcode_value,
        "result": row.result,
        "confidence": row.confidence,
        "barcode_type": row.barcode_type,
        "processing_time_ms": row.processing_time_ms,
        "detection_time_ms": row.detection_time_ms,
        "database_time_ms": row.database_time_ms,
        "device_name": row.device_name,
        "source": row.source,
        "error_code": row.error_code,
    }


def _txn_row(row: Any) -> dict[str, Any]:
    student = row.student
    return {
        "created_at": row.created_at,
        "reference_number": row.reference_number,
        "student_number": getattr(student, "student_id", None),
        "student_name": student.full_name if student else None,
        "course": getattr(student, "course", None),
        "transaction_type": row.transaction_type,
        "amount": row.amount,
        "quantity": row.quantity,
        "status": row.status,
        "cashier_username": getattr(row, "cashier_username", None),
        "reference_note": row.reference_note,
    }


# ``get_students`` is imported for the dependency graph documentation in
# docs/api-documentation.md (photo endpoint lives in routes_students).
_ = get_students
