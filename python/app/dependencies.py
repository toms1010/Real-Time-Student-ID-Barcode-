"""FastAPI dependency wiring.

Two jobs:

1. **Service singletons** - one repository/service instance per process, so the
   SQLite connection pool and the in-memory cooldown map are shared.
2. **Authorisation dependencies** - ``current_user``, ``require_permission`` and
   the shortcut helpers used by the routes.

``security.require_login = false`` is a documented kiosk mode: the API then
accepts an unauthenticated caller with the ``operator`` role, which is why
:func:`current_user` never raises when that flag is off.  The UI still shows the
login screen, but a locked-down kiosk build can run it in a dedicated browser
profile.
"""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, Request

from app.database.repository import Repository
from app.services.auth_service import ROLE_PERMISSIONS, AuthService, AuthenticatedUser
from app.services.report_service import ReportService
from app.services.scan_service import ScanService
from app.services.settings_service import SettingsService
from app.services.student_service import StudentService
from app.services.transaction_service import TransactionService
from app.services.verification_service import VerificationService
from app.utils.config import AppConfig, get_config
from app.utils.errors import AuthError, ErrorCode

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------
_repo: Repository | None = None
_auth: AuthService | None = None
_students: StudentService | None = None
_verification: VerificationService | None = None
_scans: ScanService | None = None
_transactions: TransactionService | None = None
_reports: ReportService | None = None
_settings: SettingsService | None = None


def get_repo() -> Repository:
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo


def get_auth() -> AuthService:
    global _auth
    if _auth is None:
        _auth = AuthService(repo=get_repo(), config=get_config())
    return _auth


def get_students() -> StudentService:
    global _students
    if _students is None:
        _students = StudentService(repo=get_repo(), config=get_config())
    return _students


def get_verification() -> VerificationService:
    global _verification
    if _verification is None:
        _verification = VerificationService(students=get_students(), config=get_config())
    return _verification


def get_scans() -> ScanService:
    global _scans
    if _scans is None:
        _scans = ScanService(
            repo=get_repo(), verification=get_verification(), config=get_config()
        )
    return _scans


def get_transactions() -> TransactionService:
    global _transactions
    if _transactions is None:
        _transactions = TransactionService(repo=get_repo())
        # Share the scan cooldown tracker: one policy, one place.
        _transactions.bind_cooldown(get_scans().cooldowns)
    return _transactions


def get_reports() -> ReportService:
    global _reports
    if _reports is None:
        _reports = ReportService(repo=get_repo(), config=get_config())
    return _reports


def get_settings() -> SettingsService:
    global _settings
    if _settings is None:
        _settings = SettingsService(repo=get_repo(), config=get_config())
    return _settings


def reset_services() -> None:
    """Clear every singleton (tests, and the CLI when it switches databases)."""
    global _repo, _auth, _students, _verification, _scans, _transactions, _reports, _settings
    _repo = _auth = _students = _verification = None
    _scans = _transactions = _reports = _settings = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Request-scoped values
# ---------------------------------------------------------------------------
def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


def current_user(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> AuthenticatedUser:
    """Resolve the caller from a bearer token, else from the session cookie.

    The header wins over the cookie on purpose: an API client (the C++ scanner,
    a test, ``curl -H``) that sends an explicit token must not be silently
    overridden by whatever session cookie the client happens to hold.
    """
    token: str | None = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip() or None
    if not token:
        token = request.cookies.get(config.security.cookie_name) or None
    if token:
        user = auth.resolve_token(token)
        if user is not None:
            return user
        if not config.security.require_login:
            pass
        else:
            raise AuthError(ErrorCode.AUTH_EXPIRED.user_message, code=ErrorCode.AUTH_EXPIRED)
    if not config.security.require_login:
        # Documented kiosk mode.
        return AuthenticatedUser(
            id=0, username="kiosk", full_name="Kiosk session", role="admin",
            must_change_password=False,
        )
    raise AuthError(ErrorCode.AUTH_REQUIRED.user_message, code=ErrorCode.AUTH_REQUIRED)


def require_permission(*permissions: str) -> Callable[..., AuthenticatedUser]:
    """Dependency factory: ``user = Depends(require_permission('students:write'))``."""

    def dependency(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> AuthenticatedUser:
        user.require(*permissions)
        return user

    return dependency


def require_role(*roles: str) -> Callable[..., AuthenticatedUser]:
    allowed = set(roles)

    def dependency(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> AuthenticatedUser:
        if user.role not in allowed:
            from app.utils.errors import ForbiddenError

            raise ForbiddenError(
                f"this action requires one of: {', '.join(sorted(allowed))}"
            )
        return user

    return dependency


def all_permissions() -> dict[str, list[str]]:
    return {role: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()}
