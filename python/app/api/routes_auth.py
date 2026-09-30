"""Authentication routes: login, logout, session info, user administration."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.dependencies import all_permissions, client_ip, current_user, get_auth, require_role
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    UserCreateRequest,
    UserOut,
    UserUpdateRequest,
)
from app.schemas.common import MessageResponse, PageMeta, Paged
from app.services.auth_service import AuthService, AuthenticatedUser
from app.utils.config import AppConfig, get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_cookie(response: Response, config: AppConfig, token: str, expires_at: str) -> None:
    """Attach the session cookie.

    ``httponly`` + ``samesite='strict'`` means the browser will not expose the
    token to JavaScript, so an XSS bug cannot exfiltrate the session.
    ``secure`` is enabled through configuration when the app is behind HTTPS.
    """
    response.set_cookie(
        key=config.security.cookie_name,
        value=token,
        httponly=True,
        samesite="strict",
        secure=config.security.cookie_secure,
        max_age=config.security.session_ttl_minutes * 60,
        path="/",
    )
    response.headers["X-Session-Expires-At"] = expires_at


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"description": "Invalid credentials"}},
    summary="Sign in and receive a session cookie",
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> LoginResponse:
    result = auth.login(
        payload.username,
        payload.password,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip(request),
    )
    _set_cookie(response, config, result.token, result.expires_at)
    return LoginResponse(
        token=result.token,
        expires_at=result.expires_at,
        user=UserOut(
            id=result.user.id,
            username=result.user.username,
            full_name=result.user.full_name,
            role=result.user.role,  # type: ignore[arg-type]
            is_active=True,
            must_change_password=result.user.must_change_password,
        ),
        permissions=sorted(result.user.permissions),
    )


@router.post("/logout", response_model=MessageResponse, summary="End the current session")
def logout(
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> MessageResponse:
    token = request.cookies.get(config.security.cookie_name)
    auth.logout(token)
    response.delete_cookie(config.security.cookie_name, path="/")
    return MessageResponse(message="Signed out")


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=user.role,  # type: ignore[arg-type]
        is_active=True,
        must_change_password=user.must_change_password,
    )


@router.get("/permissions", summary="Role -> permission catalogue")
def permissions() -> dict[str, list[str]]:
    return all_permissions()


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change your own password",
)
def change_password(
    payload: PasswordChangeRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    auth: Annotated[AuthService, Depends(get_auth)],
) -> MessageResponse:
    auth.change_password(user, payload.current_password, payload.new_password)
    return MessageResponse(message="Password updated. Please sign in again.")


@router.get(
    "/users",
    response_model=Paged[UserOut],
    dependencies=[Depends(require_role("admin"))],
    summary="List users",
)
def list_users(auth: Annotated[AuthService, Depends(get_auth)]) -> Paged[UserOut]:
    users = auth.repo.users.list()
    return Paged[UserOut](
        items=[
            UserOut(
                id=u.id or 0,
                username=u.username,
                full_name=u.full_name,
                role=u.role,  # type: ignore[arg-type]
                is_active=u.is_active,
                must_change_password=u.must_change_password,
                last_login_at=u.last_login_at,
                created_at=u.created_at,
            )
            for u in users
        ],
        page=PageMeta(total=len(users), limit=len(users) or 1, offset=0),
    )


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("admin"))],
    summary="Create a user",
)
def create_user(
    payload: UserCreateRequest,
    auth: Annotated[AuthService, Depends(get_auth)],
) -> UserOut:
    user = auth.create_user(
        username=payload.username,
        password=payload.password,
        role=payload.role,
        full_name=payload.full_name,
        must_change_password=payload.must_change_password,
    )
    return UserOut(
        id=user.id or 0,
        username=user.username,
        full_name=user.full_name,
        role=user.role,  # type: ignore[arg-type]
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
    )


@router.put(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(require_role("admin"))],
    summary="Update a user (role, status, password)",
)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    auth: Annotated[AuthService, Depends(get_auth)],
) -> UserOut:
    from app.utils.security import hash_password

    if payload.role is not None:
        auth.set_role(user, user_id, payload.role)
    if payload.is_active is not None:
        auth.set_active(user, user_id, payload.is_active)
    if payload.password is not None:
        auth.repo.users.set_password(user_id, hash_password(payload.password), must_change=True)
    updated = auth.repo.users.get(user_id)
    if updated is None:
        from app.utils.errors import NotFoundError

        raise NotFoundError(f"user {user_id} not found")
    return UserOut(
        id=updated.id or 0,
        username=updated.username,
        full_name=updated.full_name or payload.full_name,
        role=updated.role,  # type: ignore[arg-type]
        is_active=updated.is_active,
        must_change_password=updated.must_change_password,
        last_login_at=updated.last_login_at,
        created_at=updated.created_at,
    )
