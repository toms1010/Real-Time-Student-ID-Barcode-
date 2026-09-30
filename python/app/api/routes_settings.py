"""System settings, health, audit log and database backup routes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from app.dependencies import current_user, get_auth, get_repo, get_settings
from app.schemas.common import MessageResponse
from app.schemas.report import SettingsOut, SettingsUpdate
from app.schemas.scan import WorkflowActionIn
from app.services.auth_service import AuthService, AuthenticatedUser
from app.services.settings_service import SettingsService
from app.utils.config import AppConfig, get_config
from app.utils.errors import (
    AppError,
    ErrorCode,
    NotFoundError,
    ValidationError,
    WorkflowConflictError,
)
from app.utils.logger import get_logger
from app.utils.timeutil import format_iso, utc_now, utc_now_iso

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["system"])


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health", summary="Liveness and dependency check")
def health() -> dict[str, Any]:
    """Unauthenticated on purpose: scripts and the scanner poll this.

    It reveals only whether the service and its database are reachable; no
    configuration, no data, no version of the schema.
    """
    config = get_config()
    database = get_repo().db
    db_health = database.health_check()
    return {
        "status": "ok" if db_health.get("status") in {"ok", "degraded"} else "error",
        "service": config.application.name,
        "version": config.application.version,
        "environment": config.application.environment,
        "database": db_health,
        "offline": True,
        "time": utc_now_iso(),
    }


@router.get("/info", summary="Non-sensitive runtime information")
def info(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    config = get_config()
    return {
        "application": config.application.name,
        "version": config.application.version,
        "environment": config.application.environment,
        "api": {"host": config.api.host, "port": config.api.port},
        "camera": {
            "width": config.camera.width,
            "height": config.camera.height,
            "fps": config.camera.fps,
        },
        "scanner": {
            "cooldown_ms": config.scanner.cooldown_ms,
            "confidence_threshold": config.scanner.confidence_threshold,
            "allowed_formats": list(config.scanner.allowed_formats),
            "payload_pattern": config.scanner.payload_pattern,
            "stream_source": config.scanner.stream_source,
        },
        "config_file": str(config.config_file) if config.config_file else None,
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@router.get("/settings", response_model=list[SettingsOut], summary="List runtime settings")
def get_all_settings(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    settings: Annotated[SettingsService, Depends(get_settings)],
) -> list[SettingsOut]:
    user.require("settings:read")
    return settings.list()


@router.get("/settings/public", summary="Settings the UI needs before signing in")
def public_settings(
    settings: Annotated[SettingsService, Depends(get_settings)],
) -> dict[str, Any]:
    """Used for the login screen: application name, theme, and whether login is
    required at all.  No other configuration is exposed."""
    config = get_config()
    snapshot = settings.public_snapshot()
    return {
        "application_name": snapshot.get("application_name"),
        "theme": snapshot.get("theme", "dark"),
        "require_login": config.security.require_login,
        "version": config.application.version,
    }


@router.put("/settings", response_model=list[SettingsOut], summary="Update runtime settings")
def update_settings(
    payload: SettingsUpdate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    settings: Annotated[SettingsService, Depends(get_settings)],
) -> list[SettingsOut]:
    user.require("settings:write")
    return settings.update(payload.settings, actor=user, reason=payload.reason)


@router.delete("/settings", response_model=list[SettingsOut],
               summary="Reset settings to the config.yaml values")
def reset_settings(
    keys: Annotated[list[str], Query()],
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    settings: Annotated[SettingsService, Depends(get_settings)],
) -> list[SettingsOut]:
    user.require("settings:write")
    return settings.reset(keys, actor=user)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@router.get("/audit", summary="Audit trail (admin only)")
def audit_log(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    action: str | None = Query(default=None, max_length=64),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    user.require("audit:read")
    rows, total = get_repo().audit.list(action=action, start=start, end=end,
                                        limit=limit, offset=offset)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
@router.post("/maintenance/backup", response_model=MessageResponse,
             summary="Create a consistent backup of the database")
def backup_database(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> MessageResponse:
    """Uses SQLite's online backup API so the copy is consistent even while the
    scanner is writing."""
    user.require("backup")
    database = get_repo().db
    if not database.exists:
        raise NotFoundError("there is no database to back up")

    target_dir = config.resolve("backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"student_id_system-{utc_now():%Y%m%d-%H%M%S}.db"
    source_connection = database.connect()
    backup_connection = sqlite3.connect(target)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
    logger.info("database backup written to %s", target.name)
    get_repo().audit.log(action="database.backup", user_id=user.id, username=user.username,
                         entity_type="database", details={"file": target.name})
    return MessageResponse(message=f"Backup created: backups/{target.name}")


@router.post("/maintenance/cleanup", response_model=MessageResponse,
             summary="Purge expired sessions and old audit rows")
def cleanup(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    auth: Annotated[AuthService, Depends(get_auth)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> MessageResponse:
    user.require("settings:write")
    sessions = auth.purge_expired_sessions()
    cutoff = format_iso(utc_now() - timedelta(days=365))
    audits = get_repo().audit.purge_older_than(cutoff)  # type: ignore[arg-type]
    logger.info("maintenance: removed %d sessions and %d audit rows", sessions, audits)
    return MessageResponse(message=f"Removed {sessions} expired sessions and {audits} audit rows")


@router.get("/maintenance/files", summary="List database and backup files")
def list_files(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> dict[str, Any]:
    user.require("backup")
    repo_path = config.database_path
    backup_dir = config.resolve("backups")
    backups = sorted(backup_dir.glob("*.db"), reverse=True) if backup_dir.is_dir() else []
    return {
        "database": {
            "path": str(repo_path),
            "exists": repo_path.is_file(),
            "size_bytes": repo_path.stat().st_size if repo_path.is_file() else 0,
        },
        "backups": [
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "modified": format_iso(
                    datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                ),
            }
            for p in backups[:50]
        ],
    }


@router.get("/scanner/state", summary="Live pipeline state (states, FPS, timings)")
def scanner_state(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    """Feeds the status bar: state, FPS, processing time, last result.

    The payload embeds the full workflow under ``workflow`` so the cashier
    screen needs one poll rather than two.  ``state`` stays at the top level
    for clients that predate the workflow object.
    """
    from app.vision.pipeline import get_pipeline

    pipeline = get_pipeline()
    snapshot = pipeline.snapshot()
    workflow = snapshot.get("workflow") or {}
    if not snapshot.get("camera_connected") and not workflow.get("error_code"):
        workflow["error_message"] = workflow.get("error_message") or (
            "Camera unavailable. Please check the webcam connection."
        )
        snapshot["error_message"] = workflow["error_message"]
    return snapshot


@router.get("/scanner/workflow", summary="The scan workflow state, its actions and its history")
def scanner_workflow(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    """The canonical state machine as the UI should see it.

    Includes ``actions`` (what the cashier may do right now) and the full state
    table, so the frontend never hard-codes a transition rule.
    """
    from app.services.scan_workflow_service import get_workflow

    workflow = get_workflow()
    return {
        "success": True,
        **workflow.snapshot(),
        "states": workflow.table(),
    }


@router.post("/scanner/workflow", summary="Apply a cashier action to the scan workflow")
def scanner_workflow_action(
    payload: Annotated[WorkflowActionIn, Body()],
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    """Advance, retry or cancel the workflow.

    The action is validated against the machine, so an impossible request (for
    example *proceed* while a transaction is still being saved) is rejected with
    409 ``INVALID_STATE_TRANSITION`` instead of quietly doing nothing.
    """
    from app.services.scan_workflow_service import get_workflow
    from app.vision.pipeline import get_pipeline

    workflow = get_workflow()
    action = payload.action
    if not workflow.may(action):
        raise WorkflowConflictError(
            f"'{action}' is not available while the scanner is in "
            f"{workflow.state}. {workflow.message}",
            details={"state": str(workflow.state), "action": action,
                     "actions": workflow.actions},
        )

    pipeline = get_pipeline()
    if action in {"START_CAMERA", "RETRY_CAMERA"}:
        started = pipeline.start()
        if not started:
            # The pipeline has already entered CAMERA_ERROR with the device code.
            raise AppError(
                pipeline.error_code or ErrorCode.CAMERA_NOT_FOUND,
                pipeline.error_message or ErrorCode.CAMERA_NOT_FOUND.user_message,
            )
    elif action in {"STOP_CAMERA", "CANCEL", "ACKNOWLEDGE"}:
        pipeline.stop()
    elif action in {"RESCAN", "SCAN_AGAIN"}:
        if pipeline.running:
            workflow.resume_scanning(f"cashier chose {action}")
        else:
            pipeline.start()
    elif action == "PROCEED":
        workflow.ready_for_transaction()
    elif action == "RETRY_LOOKUP":
        barcode = workflow.snapshot().get("barcode") or ""
        if not barcode:
            raise ValidationError("there is no barcode to look up again")
        # Re-run the same lookup through the same path the camera uses, so the
        # retry produces a real state walk and a real record - never a cached
        # or invented one.
        from app.dependencies import get_scans

        workflow.scan_payload(barcode, scans=get_scans(), source="api",
                              device_name="web-retry")
    elif action == "RETRY_TRANSACTION":
        workflow.retry_transaction()

    return {"success": True, "action": action, "workflow": workflow.snapshot()}


@router.post("/scanner/start", summary="Start the Python capture pipeline")
def start_scanner(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    from app.vision.pipeline import get_pipeline

    pipeline = get_pipeline()
    if pipeline.running:
        return {"success": True, "message": "scanner already running",
                "state": pipeline.snapshot()}
    started = pipeline.start()
    return {
        "success": started,
        "message": "scanner started" if started else (pipeline.error_message or "could not start"),
        "state": pipeline.snapshot(),
    }


@router.post("/scanner/stop", summary="Stop the Python capture pipeline")
def stop_scanner(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> dict[str, Any]:
    from app.vision.pipeline import get_pipeline

    pipeline = get_pipeline()
    pipeline.stop()
    return {"success": True, "message": "scanner stopped", "state": pipeline.snapshot()}
