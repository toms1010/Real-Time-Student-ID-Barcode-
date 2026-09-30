"""Report, dashboard and export schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from app.schemas.common import ApiModel

ReportPeriod = Literal["daily", "weekly", "monthly", "custom", "all"]
ExportFormat = Literal["csv", "xlsx", "pdf", "json"]


class ReportRequest(ApiModel):
    period: ReportPeriod = "daily"
    date: str | None = Field(default=None, description="Anchor date, YYYY-MM-DD")
    start: str | None = Field(default=None, description="Custom period start (ISO-8601)")
    end: str | None = Field(default=None, description="Custom period end (ISO-8601)")
    student_id: str | None = None
    timezone: str | None = Field(default=None, description="Overrides reports.timezone")

    def resolve_range(self, default_timezone: str = "UTC") -> tuple[str, str, str]:
        """Return ``(start_iso, end_iso, label)`` for this request.

        Period boundaries are computed in ``self.timezone`` and returned as UTC
        instants so the SQL range filter stays correct across timezones.
        """
        from datetime import timedelta

        from app.utils.timeutil import format_iso, parse_iso, to_local, utc_now

        tz_name = self.timezone or default_timezone
        anchor_local = to_local(parse_iso(self.date) or utc_now(), tz_name)
        assert anchor_local is not None

        if self.period == "custom" and (self.start and self.end):
            start = parse_iso(self.start)
            end = parse_iso(self.end)
            if start is None or end is None:
                from app.utils.errors import ValidationError

                raise ValidationError("custom period requires valid start and end timestamps")
            return format_iso(start), format_iso(end), f"{self.start} .. {self.end}"  # type: ignore[return-value]

        if self.period == "daily":
            start_local = anchor_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
            label = start_local.strftime("%Y-%m-%d")
        elif self.period == "weekly":
            start_local = (anchor_local - timedelta(days=anchor_local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_local = start_local + timedelta(days=7)
            label = f"{start_local:%Y-%m-%d} .. {(end_local - timedelta(days=1)):%Y-%m-%d}"
        elif self.period == "monthly":
            start_local = anchor_local.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            end_local = (start_local + timedelta(days=32)).replace(day=1)
            label = f"{start_local:%Y-%m}"
        else:  # "all"
            start_local = anchor_local.replace(day=1, month=1, hour=0, minute=0,
                                              second=0, microsecond=0)
            end_local = anchor_local + timedelta(days=1)
            label = "all time"

        from zoneinfo import ZoneInfo

        try:
            zone = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - unknown zone must not break a report
            zone = ZoneInfo("UTC")
        return (
            format_iso(start_local.astimezone(ZoneInfo("UTC"))),  # type: ignore[arg-type]
            format_iso(end_local.astimezone(ZoneInfo("UTC"))),  # type: ignore[arg-type]
            label,
        )


class DailyRow(ApiModel):
    scan_date: str
    total_scans: int
    verified: int
    unknown_id: int
    invalid_barcode: int
    duplicate_scan: int
    unsupported_format: int
    errors: int
    avg_processing_ms: float | None = None
    max_processing_ms: float | None = None
    avg_confidence: float | None = None

    model_config = {"extra": "ignore"}


class DailyReport(ApiModel):
    period: str
    label: str
    start: str
    end: str
    timezone: str
    totals: dict[str, Any]
    days: list[DailyRow]
    top_students: list[dict[str, Any]] = Field(default_factory=list)
    unknown_barcodes: list[dict[str, Any]] = Field(default_factory=list)
    hourly_profile: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: str


class DashboardSummary(ApiModel):
    """GET /api/reports/summary - the dashboard counter tiles."""

    students_total: int
    students_active: int
    students_inactive: int
    scans_total: int
    scans_today: int
    verified_today: int
    errors_today: int
    unknown_today: int
    duplicates_today: int
    transactions_today: int
    revenue_today: float
    avg_processing_ms: float | None = None
    last_scan: dict[str, Any] | None = None
    trend_7d: list[dict[str, Any]] = Field(default_factory=list)
    hourly_today: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: str


class PerformanceReport(ApiModel):
    period: str
    label: str
    start: str
    end: str
    frames_observed: int = Field(..., description="scan_logs rows in the period")
    scans: int
    verified: int
    failed: int
    avg_processing_ms: float | None = None
    min_processing_ms: float | None = None
    max_processing_ms: float | None = None
    avg_detection_ms: float | None = None
    avg_database_ms: float | None = None
    p95_processing_ms: float | None = None
    avg_confidence: float | None = None
    slowest: list[dict[str, Any]] = Field(default_factory=list)
    by_device: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: str


class ExportDescriptor(ApiModel):
    filename: str
    format: ExportFormat
    rows: int
    size_bytes: int
    download_url: str


class ExportRequest(ApiModel):
    report: Literal["daily", "scans", "transactions", "unknown_barcodes", "student_history",
                    "performance"]
    format: ExportFormat = "xlsx"
    start: str | None = None
    end: str | None = None
    student_id: Annotated[str, Field(max_length=64)] | None = None
    result: str | None = None
    limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000


class SettingsOut(ApiModel):
    key: str
    value: str | None = None
    value_type: str
    category: str
    description: str | None = None
    updated_at: str | None = None


class SettingsUpdate(ApiModel):
    settings: dict[str, str | int | float | bool]
    reason: str | None = Field(default=None, max_length=200)
