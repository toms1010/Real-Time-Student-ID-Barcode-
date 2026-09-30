"""Authentication, session management and role-based authorisation.

* Argon2id password verification (see ``app.utils.security``).
* Opaque bearer tokens stored as SHA-256 hashes (ADR-005).
* Per-username lockout after ``max_login_attempts`` failures.
* Role -> permission mapping, checked by ``app.dependencies.require_role``.

Roles
-----
``admin``   full access, including users, settings, hard delete and backup
``cashier`` scan, verify, record transactions, read students
``staff``   scan, verify, read students, view history
``operator`` legacy alias of ``cashier`` (kept for schema compatibility)
``viewer``  read-only reports and history
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.database.repository import Repository
from app.utils.config import AppConfig, get_config
from app.utils.errors import AuthError, ErrorCode, ForbiddenError, ValidationError
from app.utils.logger import get_logger
from app.utils.security import (
    generate_session_token,
    hash_password,
    hash_token,
    needs_rehash,
    validate_password_strength,
    validate_username,
    verify_password,
)
from app.utils.timeutil import format_iso, parse_iso, utc_now, utc_now_iso

logger = get_logger(__name__)

#: Permission catalogue.  Routes declare the permissions they need; the UI
#: renders the navigation from the same list (GET /api/auth/permissions).
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "scan", "verify", "transaction:create", "transaction:cancel",
        "students:read", "students:write", "students:delete", "barcodes:write",
        "scans:read", "transactions:read", "reports:read", "reports:export",
        "settings:read", "settings:write", "users:read", "users:write",
        "backup", "audit:read",
    },
    "cashier": {
        "scan", "verify", "transaction:create", "transaction:cancel",
        "students:read", "scans:read", "transactions:read",
        "reports:read", "reports:export", "settings:read",
    },
    "staff": {"scan", "verify", "students:read", "scans:read", "reports:read", "settings:read"},
    "operator": {
        "scan", "verify", "transaction:create", "students:read", "scans:read",
        "reports:read", "settings:read",
    },
    "viewer": {"students:read", "scans:read", "transactions:read", "reports:read", "settings:read"},
}


@dataclass(slots=True)
class AuthenticatedUser:
    """What a request handler needs to know about the caller."""

    id: int
    username: str
    full_name: str | None
    role: str
    must_change_password: bool
    session_id: int | None = None
    expires_at: str | None = None

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, *permissions: str) -> None:
        missing = [p for p in permissions if not self.has(p)]
        if missing:
            raise ForbiddenError(
                f"role '{self.role}' is missing permission(s): {', '.join(missing)}"
            )


@dataclass(slots=True)
class LoginResult:
    token: str
    expires_at: str
    user: AuthenticatedUser


class AuthService:
    def __init__(self, repo: Repository | None = None, config: AppConfig | None = None) -> None:
        self.repo = repo or Repository()
        self.config = config or get_config()

    # -- login -------------------------------------------------------------
    def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> LoginResult:
        """Authenticate and issue a session token.

        Failure modes are deliberately indistinguishable to the client
        (``AUTH_FAILED``) so the endpoint cannot be used to enumerate usernames;
        the audit log keeps the distinction.
        """
        username = (username or "").strip()
        security = self.config.security
        now = utc_now()

        # Throttle before touching the password hash: Argon2 is intentionally
        # slow, so an attacker must not be able to make us run it repeatedly.
        if self._is_locked(username, now):
            self.repo.users.record_attempt(username, client_ip, False)
            self.repo.audit.log(action="auth.login_blocked", username=username,
                                details={"reason": "too_many_failures"}, ip_address=client_ip)
            raise AuthError(ErrorCode.ACCOUNT_LOCKED.user_message, code=ErrorCode.ACCOUNT_LOCKED)

        user = self.repo.users.get_by_username(username)
        valid = verify_password(user.password_hash, password) if user else False

        if not user or not valid:
            self.repo.users.record_attempt(username, client_ip, False)
            self.repo.audit.log(
                action="auth.login_failed",
                username=username,
                entity_type="user",
                details={"reason": "invalid_credentials" if user else "unknown_user"},
                ip_address=client_ip,
            )
            logger.warning("failed login for username %r from %s", username, client_ip)
            raise AuthError(ErrorCode.AUTH_FAILED.user_message)

        if not user.is_active:
            self.repo.users.record_attempt(username, client_ip, False)
            self.repo.audit.log(action="auth.login_denied", username=username,
                                entity_type="user", details={"reason": "inactive"},
                                ip_address=client_ip)
            raise AuthError("This account has been deactivated.")

        self.repo.users.clear_failures(username)
        self.repo.users.touch_login(user.id or 0)

        # Transparent upgrade of weaker legacy hashes.
        if needs_rehash(user.password_hash):
            try:
                self.repo.users.set_password(user.id or 0, hash_password(password))
                logger.info("rehashed password for %s with current parameters", username)
            except ValidationError:  # pragma: no cover - demo password is policy-compliant
                pass

        token = generate_session_token()
        expires_at = format_iso(now + timedelta(minutes=security.session_ttl_minutes))  # type: ignore[arg-type]
        self.repo.users.create_session(
            user_pk=user.id or 0,
            token_hash=hash_token(token),
            expires_at=expires_at,  # type: ignore[arg-type]
            user_agent=(user_agent or "")[:200] or None,
            client_ip=client_ip,
        )
        self.repo.audit.log(action="auth.login", user_id=user.id, username=username,
                            entity_type="user", entity_id=str(user.id), ip_address=client_ip)
        logger.info("user %s signed in (role=%s)", username, user.role)

        return LoginResult(
            token=token,
            expires_at=expires_at,  # type: ignore[arg-type]
            user=AuthenticatedUser(
                id=user.id or 0,
                username=user.username,
                full_name=user.full_name,
                role=user.role,
                must_change_password=user.must_change_password,
                expires_at=expires_at,
            ),
        )

    def _is_locked(self, username: str, now) -> bool:
        security = self.config.security
        window_start = format_iso(now - timedelta(seconds=security.login_lockout_seconds))  # type: ignore[arg-type]
        failures = self.repo.users.recent_failures(username, window_start)  # type: ignore[arg-type]
        return failures >= security.max_login_attempts

    # -- session resolution -------------------------------------------------
    def resolve_token(self, token: str | None) -> AuthenticatedUser | None:
        if not token:
            return None
        found = self.repo.users.find_session(hash_token(token))
        if not found:
            return None
        session, user = found
        expires_at = parse_iso(session.expires_at)
        if expires_at is None or expires_at <= utc_now():
            # Expired sessions are revoked on sight so the table cannot grow
            # unboundedly between purges.
            self.repo.users.revoke_session(session.token_hash)
            return None
        if not user.is_active:
            return None
        return AuthenticatedUser(
            id=user.id or 0,
            username=user.username,
            full_name=user.full_name,
            role=user.role,
            must_change_password=user.must_change_password,
            session_id=session.id,
            expires_at=session.expires_at,
        )

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        self.repo.users.revoke_session(hash_token(token))
        return True

    def logout_all(self, user_pk: int) -> int:
        self.repo.users.revoke_all_sessions(user_pk)
        self.repo.audit.log(action="auth.logout_all", user_id=user_pk)
        return 1

    # -- password management ------------------------------------------------
    def change_password(self, user: AuthenticatedUser, current: str, new: str) -> None:
        row = self.repo.users.get(user.id)
        if row is None or not verify_password(row.password_hash, current):
            raise AuthError("The current password is incorrect.")
        if current == new:
            raise ValidationError("The new password must be different from the current one")
        validate_password_strength(new)
        self.repo.users.set_password(user.id, hash_password(new), must_change=False)
        # Force other devices to sign in again.
        self.repo.users.revoke_all_sessions(user.id)
        self.repo.audit.log(action="auth.password_changed", user_id=user.id, username=user.username)

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = "operator",
        full_name: str | None = None,
        must_change_password: bool = True,
    ):
        validate_username(username)
        validate_password_strength(password)
        user = self.repo.users.create(
            username=username,
            password_hash=hash_password(password),
            role=role,
            full_name=full_name,
            must_change_password=must_change_password,
        )
        self.repo.audit.log(action="user.created", username=username, entity_type="user",
                            entity_id=str(user.id), details={"role": role})
        return user

    def set_role(self, actor: AuthenticatedUser, user_pk: int, role: str) -> None:
        self.repo.users.set_role(user_pk, role)
        self.repo.audit.log(action="user.role_changed", user_id=actor.id, username=actor.username,
                            entity_type="user", entity_id=str(user_pk), details={"role": role})

    def set_active(self, actor: AuthenticatedUser, user_pk: int, active: bool) -> None:
        if user_pk == actor.id and not active:
            raise ValidationError("You cannot deactivate your own account")
        self.repo.users.set_active(user_pk, active)
        if not active:
            self.repo.users.revoke_all_sessions(user_pk)
        self.repo.audit.log(action="user.activated" if active else "user.deactivated",
                            user_id=actor.id, username=actor.username, entity_type="user",
                            entity_id=str(user_pk))

    def purge_expired_sessions(self) -> int:
        return self.repo.users.purge_expired_sessions()


_auth_service: AuthService | None = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service


def reset_auth_service() -> None:  # used by tests
    global _auth_service
    _auth_service = None
