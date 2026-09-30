"""Scan routes - the endpoint the C++ scanner and the browser UI both call."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, status

from app.dependencies import current_user, get_scans
from app.schemas.common import PageMeta, Paged
from app.schemas.scan import ScanOut, ScanRequest, ScanSummary, ScanVerification
from app.services.auth_service import AuthenticatedUser
from app.services.scan_service import ScanService
from app.utils.config import get_config

#: Paging bounds come from the configuration file, resolved once at import.
_CONFIG = get_config()
DEFAULT_PAGE_SIZE = _CONFIG.reports.default_page_size
MAX_PAGE_SIZE = _CONFIG.reports.max_page_size
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scans", tags=["scans"])


@router.post(
    "",
    response_model=ScanVerification,
    status_code=status.HTTP_200_OK,
    summary="Verify a decoded barcode and record the attempt",
    responses={
        200: {"description": "Verification result (VERIFIED, UNKNOWN_ID, ...)"},
        422: {"description": "The payload is not a valid student number"},
    },
)
def create_scan(
    payload: Annotated[ScanRequest, Body()],
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
) -> ScanVerification:
    """The core endpoint of the whole system.

    Called once per decoded symbol by ``student_id_scanner`` (C++) and by the
    Python pipeline.  The response is the same shape in both cases, so the UI
    does not care which produced it.

    When ``workflow=true`` (the default) the request also advances the scan
    state machine, so manual entry and a replay move through the same states a
    camera scan does.  Clients that drive the pipeline themselves - the C++
    scanner - pass ``workflow=false`` to avoid fighting the capture thread over
    the machine.
    """
    request: dict[str, Any] = payload.model_dump(exclude={"operator_id"})
    request["operator_id"] = user.id or None
    if payload.source == "cpp" and not payload.advance_workflow:
        # The native scanner owns the workflow when it owns the camera.
        return scans.process_scan(request, operator_id=user.id or None)

    from app.services.scan_workflow_service import get_workflow
    from app.utils.errors import WorkflowConflictError

    response = get_workflow().scan_payload(
        str(payload.barcode),
        scans=scans,
        barcode_type=payload.barcode_type,
        confidence=payload.confidence,
        source=payload.source,
        device_name=payload.device_name,
        operator_id=user.id or None,
        processing_time_ms=payload.processing_time_ms,
        detection_time_ms=payload.detection_time_ms,
    )
    if response is None:
        # The workflow could not accept the payload - most likely a transaction
        # is still being saved. 409 rather than a silent no-op, so the caller
        # knows nothing was looked up and nothing was recorded.
        workflow = get_workflow()
        raise WorkflowConflictError(
            f"The scanner is busy ({workflow.state}) and cannot accept a scan right now.",
            details={"state": str(workflow.state)},
        )
    return response


@router.get(
    "",
    response_model=Paged[ScanOut],
    summary="Scan history with filters and paging",
)
def list_scans(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
    start: str | None = Query(default=None, description="ISO-8601 inclusive lower bound"),
    end: str | None = Query(default=None, description="ISO-8601 exclusive upper bound"),
    result: str | None = Query(default=None,
                               pattern="^(VERIFIED|UNKNOWN_ID|INVALID_BARCODE|DUPLICATE_SCAN|UNSUPPORTED_FORMAT|ERROR)$"),
    search: str | None = Query(default=None, max_length=120),
    student_id: str | None = Query(default=None, max_length=64),
    device_name: str | None = Query(default=None, max_length=64),
    sort: str = Query(default="scan_time",
                      pattern="^(scan_time|student_id|result|processing_time_ms)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> Paged[ScanOut]:
    rows, total = scans.history(
        start=start, end=end, result=result, search=search, student_id=student_id,
        device_name=device_name, sort=sort, order=order, limit=limit, offset=offset,  # type: ignore[arg-type]
    )
    items = [ScanOut.from_model(row) for row in rows]
    return Paged[ScanOut](
        items=items,
        page=PageMeta(total=total, limit=limit, offset=offset,
                      has_more=offset + len(items) < total),
    )


@router.get("/current", response_model=ScanVerification | None, summary="Last scan result")
def current_scan(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
) -> ScanVerification | None:
    last = scans.last_scan()
    return ScanVerification.model_validate(last) if last else None


@router.get("/summary", response_model=ScanSummary, summary="Aggregate counters")
def scan_summary(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> ScanSummary:
    return ScanSummary.model_validate(scans.summary(start, end))


@router.get("/recent", response_model=list[ScanOut], summary="Most recent scans")
def recent_scans(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
    limit: int = Query(default=10, ge=1, le=100),
) -> list[ScanOut]:
    return [ScanOut.from_model(row) for row in scans.recent(limit)]


@router.get(
    "/{scan_id}",
    response_model=ScanOut,
    responses={404: {"description": "Unknown scan"}},
    summary="Fetch one scan row",
)
def get_scan(
    scan_id: int,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
) -> ScanOut:
    return ScanOut.from_model(scans.get(scan_id))


@router.post("/cooldown/clear", summary="Clear the in-memory duplicate suppression map")
def clear_cooldowns(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    scans: Annotated[ScanService, Depends(get_scans)],
) -> dict[str, Any]:
    user.require("settings:write")
    return {"success": True, "cleared": scans.clear_cooldowns()}
