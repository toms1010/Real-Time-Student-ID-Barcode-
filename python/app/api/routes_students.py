"""Student management routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.dependencies import current_user, get_students
from app.schemas.common import MessageResponse, PageMeta, Paged
from app.schemas.student import BarcodeCreate, BarcodeOut, StudentCreate, StudentOut, StudentUpdate
from app.services.auth_service import AuthenticatedUser
from app.services.student_service import StudentService
from app.utils.config import get_config

_CONFIG = get_config()
DEFAULT_PAGE_SIZE = _CONFIG.reports.default_page_size
MAX_PAGE_SIZE = _CONFIG.reports.max_page_size
from app.utils.errors import FileError
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/students", tags=["students"])


@router.get(
    "",
    response_model=Paged[StudentOut],
    summary="List and search students",
)
def list_students(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
    search: str | None = Query(default=None, max_length=120,
                               description="ID, name, email or barcode fragment"),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    course: str | None = Query(default=None, max_length=120),
    year_level: int | None = Query(default=None, ge=1, le=12),
    sort: str = Query(default="student_id", pattern="^(student_id|name|created_at|updated_at|status|course)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> Paged[StudentOut]:
    rows, total = students.list_students(
        search=search,
        status=status_filter,
        course=course,
        year_level=year_level,
        sort=sort,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )
    items = [StudentOut.from_model(s) for s in rows]
    return Paged[StudentOut](
        items=items,
        page=PageMeta(total=total, limit=limit, offset=offset, has_more=offset + len(items) < total),
    )


@router.get("/courses", response_model=list[str], summary="Distinct course values")
def list_courses(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> list[str]:
    return students.repo.students.distinct_courses()


@router.get(
    "/{student_id}",
    response_model=StudentOut,
    responses={404: {"description": "Unknown student"}},
    summary="Fetch one student",
)
def get_student(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> StudentOut:
    return StudentOut.from_model(students.get(student_id))


@router.post(
    "",
    response_model=StudentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a student (and their first barcode)",
)
def create_student(
    payload: StudentCreate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> StudentOut:
    user.require("students:write")
    created = students.create(payload.model_dump(), actor=user)
    return StudentOut.from_model(students.repo.students.get_by_pk(created.id or 0))  # type: ignore[arg-type]


@router.put(
    "/{student_id}",
    response_model=StudentOut,
    summary="Update a student",
)
def update_student(
    student_id: str,
    payload: StudentUpdate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> StudentOut:
    user.require("students:write")
    changes = payload.model_dump(exclude_none=True)
    return StudentOut.from_model(students.update(student_id, changes, actor=user))


@router.delete(
    "/{student_id}",
    response_model=StudentOut,
    summary="Deactivate a student (soft delete)",
)
def deactivate_student(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> StudentOut:
    user.require("students:write")
    return StudentOut.from_model(students.deactivate(student_id, actor=user))


@router.delete(
    "/{student_id}/permanent",
    response_model=MessageResponse,
    summary="Permanently delete a student (admin, no scan history)",
)
def hard_delete_student(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> MessageResponse:
    students.hard_delete(student_id, actor=user)
    return MessageResponse(message=f"{student_id} was permanently deleted")


@router.post(
    "/{student_id}/reactivate",
    response_model=StudentOut,
    summary="Reactivate a deactivated student",
)
def reactivate_student(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> StudentOut:
    user.require("students:write")
    return StudentOut.from_model(students.reactivate(student_id, actor=user))


# ---------------------------------------------------------------------------
# Barcodes
# ---------------------------------------------------------------------------
@router.get(
    "/{student_id}/barcodes",
    response_model=list[BarcodeOut],
    summary="List a student's barcodes",
)
def list_barcodes(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> list[BarcodeOut]:
    record = students.get(student_id)
    return [
        BarcodeOut(
            id=b.id or 0,
            barcode_value=b.barcode_value,
            barcode_type=b.barcode_type,
            label=b.label,
            is_primary=b.is_primary,
            retired_at=b.retired_at,
            created_at=b.created_at,
        )
        for b in students.repo.barcodes.list_for_student(record.id or 0)
    ]


@router.post(
    "/{student_id}/barcodes",
    response_model=BarcodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register an additional barcode for a student",
)
def add_barcode(
    student_id: str,
    payload: BarcodeCreate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> BarcodeOut:
    user.require("barcodes:write")
    record = students.get(student_id)
    barcode = students.add_barcode(record, payload.model_dump(), actor=user)
    return BarcodeOut(
        id=barcode.id or 0,
        barcode_value=barcode.barcode_value,
        barcode_type=barcode.barcode_type,
        label=barcode.label,
        is_primary=barcode.is_primary,
        created_at=barcode.created_at,
    )


@router.delete(
    "/{student_id}/barcodes/{barcode_id}",
    response_model=MessageResponse,
    summary="Remove one of a student's barcodes",
)
def remove_barcode(
    student_id: str,
    barcode_id: int,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> MessageResponse:
    user.require("barcodes:write")
    students.remove_barcode(student_id, barcode_id, actor=user)
    return MessageResponse(message=f"Barcode {barcode_id} removed")


# ---------------------------------------------------------------------------
# Photo
# ---------------------------------------------------------------------------
@router.post(
    "/{student_id}/photo",
    response_model=StudentOut,
    summary="Upload a student photo (optional, size and type validated)",
)
async def upload_photo(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
    file: UploadFile = File(..., description="JPEG or PNG, at most 2 MiB"),
) -> StudentOut:
    user.require("students:write")
    config = get_config()
    content = await file.read(config.security.max_upload_bytes + 1)
    if not content:
        raise FileError("The uploaded file is empty.")
    students.store_photo(student_id, file.filename or "photo.jpg", content, actor=user)
    return StudentOut.from_model(students.get(student_id))


@router.get(
    "/{student_id}/photo",
    summary="Download a student photo",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}, "description": "Photo bytes"}},
)
def get_photo(
    student_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> Response:
    path = students.photo_path(student_id)
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return Response(content=path.read_bytes(), media_type=media_type,
                    headers={"Cache-Control": "private, max-age=60"})
