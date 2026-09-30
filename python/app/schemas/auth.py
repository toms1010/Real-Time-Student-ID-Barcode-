"""Authentication and user schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import ApiModel

Role = Literal["admin", "cashier", "staff", "operator", "viewer"]


class LoginRequest(ApiModel):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserOut(ApiModel):
    id: int
    username: str
    full_name: str | None = None
    role: Role
    is_active: bool
    must_change_password: bool
    last_login_at: str | None = None
    created_at: str | None = None


class LoginResponse(ApiModel):
    success: bool = True
    token: str = Field(..., description="Opaque session token, set as an HttpOnly cookie")
    expires_at: str
    user: UserOut
    permissions: list[str] = Field(default_factory=list)


class PasswordChangeRequest(ApiModel):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=10, max_length=256)]


class UserCreateRequest(ApiModel):
    username: Annotated[str, Field(min_length=3, max_length=64,
                                   pattern=r"^[a-zA-Z0-9._-]{3,64}$")]
    password: Annotated[str, Field(min_length=10, max_length=256)]
    full_name: Annotated[str, Field(max_length=120)] | None = None
    role: Role = "operator"
    must_change_password: bool = True


class UserUpdateRequest(ApiModel):
    full_name: Annotated[str, Field(max_length=120)] | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: Annotated[str, Field(min_length=10, max_length=256)] | None = None
