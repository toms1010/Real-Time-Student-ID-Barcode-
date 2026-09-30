"""Pydantic wire schemas.

The API layer never returns ORM/SQL rows directly: repositories produce
``app.database.models`` dataclasses, services convert them to these schemas, and
FastAPI serialises the schemas.  That keeps column names from leaking into the
public contract and gives us one place to document the wire format.

Naming conventions
------------------
* Request bodies  - ``XxxCreate`` / ``XxxUpdate`` / ``XxxRequest``
* Response bodies - ``XxxOut``
* Paged responses - ``XxxListOut`` with ``items`` + ``total`` + ``page``
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every schema: forbid unknown fields so typos are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True,
                              populate_by_name=True)


class PageMeta(ApiModel):
    total: int = Field(..., ge=0, description="Total number of matching rows")
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    has_more: bool = False


class Paged(ApiModel, Generic[T]):
    items: list[T]
    page: PageMeta


class ErrorBody(ApiModel):
    code: str = Field(..., examples=["STUDENT_NOT_FOUND"])
    message: str = Field(..., examples=["Student record not found. Please verify the ID."])
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    success: bool = False
    error: ErrorBody


class MessageResponse(ApiModel):
    success: bool = True
    message: str
