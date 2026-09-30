"""Student management: CRUD, barcode registration, photo upload validation.

Business rules enforced here (not in the route handlers):

* ``student_id`` is unique and stable; changing it does **not** rewrite existing
  barcodes automatically - the service offers ``sync_primary_barcode`` so an
  administrator decides explicitly.
* Delete is a deactivate by default so the audit trail survives (ADR-009).
* Photos are optional, size-capped, extension- and content-validated, and
  stored outside the web root.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from app.database.models import Student
from app.database.repository import Repository
from app.utils.config import AppConfig, get_config
from app.utils.errors import ErrorCode, FileError, NotFoundError, ValidationError
from app.utils.logger import get_logger
from app.utils.security import ensure_within, safe_filename, validate_barcode_payload
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)

_ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png"}


class StudentService:
    def __init__(self, repo: Repository | None = None, config: AppConfig | None = None) -> None:
        self.repo = repo or Repository()
        self.config = config or get_config()

    # -- queries -----------------------------------------------------------
    def list_students(self, **kwargs: Any) -> tuple[list[Student], int]:
        return self.repo.students.list(**kwargs)

    def get(self, student_id: str) -> Student:
        """Look a student up by their human readable number."""
        student = self.repo.students.get_by_student_id(student_id)
        if student is None:
            raise NotFoundError(
                ErrorCode.STUDENT_NOT_FOUND.user_message,
                details={"student_id": student_id},
            )
        return student

    def get_by_pk(self, pk: int) -> Student:
        student = self.repo.students.get_by_pk(pk)
        if student is None:
            raise NotFoundError(f"student {pk} not found")
        return student

    def find_by_barcode(self, value: str) -> tuple[Any, Student | None]:
        """Return ``(barcode_row, student)`` for a decoded payload.

        The payload is validated against the configured pattern *before* the
        query, so a hostile string can never reach the ``WHERE`` clause.
        """
        payload = validate_barcode_payload(value, self.config.scanner.payload_pattern)
        barcode = self.repo.barcodes.find_by_value(payload)
        if barcode is None or barcode.student_id is None:
            return None, None
        return barcode, self.repo.students.get_by_pk(barcode.student_id)

    # -- writes ------------------------------------------------------------
    def create(self, data: dict[str, Any], *, actor: Any = None) -> Student:
        barcodes = data.pop("barcodes", []) or []
        primary = data.get("student_id")
        self._assert_student_id_free(primary)
        student = self.repo.students.create(data)

        # A card is printed with the student number by default (ADR-002).
        for barcode in barcodes:
            self.add_barcode(student, barcode, actor=actor)
        if not barcodes:
            self.add_barcode(
                student,
                {
                    "barcode_value": primary,
                    "barcode_type": "Code128",
                    "label": "Primary card",
                    "is_primary": True,
                },
                actor=actor,
            )
        if actor is not None:
            self.repo.audit.log(action="student.created", user_id=actor.id, username=actor.username,
                                entity_type="student", entity_id=str(student.id),
                                details={"student_id": student.student_id})
        logger.info("created student %s (pk=%s)", student.student_id, student.id)
        return self.repo.students.get_by_pk(student.id or 0)  # type: ignore[return-value]

    def _assert_student_id_free(self, student_id: str | None) -> None:
        if student_id and self.repo.students.get_by_student_id(student_id) is not None:
            raise ValidationError(f"student_id {student_id!r} already exists")

    def update(self, student_id: str, changes: dict[str, Any], *, actor: Any = None) -> Student:
        student = self.get(student_id)
        if not student.id:
            raise NotFoundError(f"student {student_id} has no internal id")

        new_number = changes.get("student_id")
        if new_number and new_number != student.student_id:
            if self.repo.students.get_by_student_id(new_number) is not None:
                raise ValidationError(f"student_id {new_number!r} already exists")

        payload = changes.get("photo_path", student.photo_path)
        self._validate_photo_path(payload)
        updated = self.repo.students.update(student.id, changes)
        if actor is not None:
            self.repo.audit.log(
                action="student.updated", user_id=actor.id, username=actor.username,
                entity_type="student", entity_id=str(student.id),
                details={"fields": sorted(changes.keys())},
            )
        return updated

    def deactivate(self, student_id: str, *, actor: Any = None) -> Student:
        student = self.get(student_id)
        self.repo.students.delete(student.id or 0, hard=False)
        if actor is not None:
            self.repo.audit.log(action="student.deactivated", user_id=actor.id,
                                username=actor.username, entity_type="student",
                                entity_id=str(student.id), details={"student_id": student_id})
        return self.repo.students.get_by_pk(student.id or 0)  # type: ignore[return-value]

    def hard_delete(self, student_id: str, *, actor: Any = None) -> None:
        """Permanent deletion.  Admin only, and refused once scans exist.

        The check is a real privacy feature: a record with scan history is
        deactivated instead, so the institution keeps the audit trail it needs
        while the student's card stops resolving.
        """
        student = self.get(student_id)
        history = self.repo.db.scalar(
            "SELECT COUNT(*) FROM scan_logs WHERE student_id = ?", (student.id,)
        )
        if history:
            raise ValidationError(
                "This student has scan history; deactivate the record instead of deleting it "
                "(data protection and audit requirements)"
            )
        if actor is not None:
            actor.require("students:delete")
        self.repo.students.delete(student.id or 0, hard=True)
        self.repo.audit.log(action="student.deleted", user_id=actor.id if actor else None,
                            username=actor.username if actor else None, entity_type="student",
                            entity_id=str(student.id), details={"student_id": student_id})
        logger.warning("permanently deleted student %s (pk=%s)", student_id, student.id)

    def reactivate(self, student_id: str, *, actor: Any = None) -> Student:
        student = self.get(student_id)
        updated = self.repo.students.reactivate(student.id or 0)
        if actor is not None:
            self.repo.audit.log(action="student.reactivated", user_id=actor.id,
                                username=actor.username, entity_type="student",
                                entity_id=str(student.id))
        return updated

    # -- barcodes ----------------------------------------------------------
    def add_barcode(self, student: Student, data: dict[str, Any], *, actor: Any = None):
        value = validate_barcode_payload(
            str(data.get("barcode_value", "")), self.config.scanner.payload_pattern
        )
        barcode = self.repo.barcodes.add(
            student.id or 0,
            value,
            barcode_type=str(data.get("barcode_type") or "Code128")[:32],
            label=(data.get("label") or None),
            is_primary=bool(data.get("is_primary")),
        )
        if actor is not None:
            self.repo.audit.log(action="barcode.registered", user_id=actor.id,
                                username=actor.username, entity_type="student_barcode",
                                entity_id=str(barcode.id),
                                details={"student_id": student.student_id, "barcode_type": barcode.barcode_type})
        return barcode

    def remove_barcode(self, student_id: str, barcode_pk: int, *, actor: Any = None) -> None:
        student = self.get(student_id)
        existing = [b for b in self.repo.barcodes.list_for_student(student.id or 0)
                    if b.id == barcode_pk]
        if not existing:
            raise NotFoundError(f"barcode {barcode_pk} does not belong to {student_id}")
        self.repo.barcodes.remove(barcode_pk)
        if actor is not None:
            self.repo.audit.log(action="barcode.removed", user_id=actor.id,
                                username=actor.username, entity_type="student_barcode",
                                entity_id=str(barcode_pk))
        logger.info("removed barcode %s from student %s", barcode_pk, student_id)

    # -- photos ------------------------------------------------------------
    def store_photo(self, student_id: str, filename: str, content: bytes, *, actor: Any = None) -> str:
        """Validate and store a student photo, then point the record at it.

        Returns the stored file name.  The database is updated here (rather than
        left to the caller) so a photo can never end up on disk without the
        student referencing it.

        Rejections (all mapped to a clear operator message):
          * larger than ``security.max_upload_bytes``          -> FILE_ERROR
          * extension not in the allow-list                    -> FILE_ERROR
          * content not a decodable JPEG/PNG                   -> FILE_ERROR
          * dimension beyond ``uploads.max_photo_dimension``   -> resized
        """
        security, uploads = self.config.security, self.config.uploads
        if len(content) > security.max_upload_bytes:
            raise FileError(
                f"Photo is too large (limit {security.max_upload_bytes // 1024} KiB).",
                details={"size": len(content)},
            )
        extension = Path(filename).suffix.lower()
        if extension not in security.allowed_photo_extensions:
            raise FileError(
                "Unsupported image type. Allowed: " + ", ".join(security.allowed_photo_extensions)
            )
        guessed = mimetypes.guess_type(filename)[0] or ""
        if guessed not in _ALLOWED_IMAGE_MIMES:
            raise FileError("Unsupported image type.")

        try:
            import io

            from PIL import Image, UnidentifiedImageError

            with Image.open(io.BytesIO(content)) as image:
                image.verify()  # structural check only
            with Image.open(io.BytesIO(content)) as image:
                fmt = (image.format or "").upper()
                if fmt not in {"JPEG", "PNG"}:
                    raise FileError("Only JPEG and PNG photos are accepted.")
                image.load()
                width, height = image.size
                limit = uploads.max_photo_dimension
                if max(width, height) > limit:
                    image.thumbnail((limit, limit))
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, format="JPEG", quality=85)
                    content = buffer.getvalue()
                    extension = ".jpg"
        except FileError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise FileError("The uploaded file is not a readable image.") from exc

        directory = self.config.photo_dir
        directory.mkdir(parents=True, exist_ok=True)
        stored_name = f"{student_id}_{utc_now_iso().replace(':', '').replace('-', '')}{extension}"
        target = directory / stored_name
        if not ensure_within(directory, target):  # pragma: no cover - defensive
            raise FileError("Refusing to write outside the upload directory.")
        target.write_bytes(content)

        # The database stores the file *name* only: the directory is
        # configuration, not data, so moving `uploads.photo_dir` must not break
        # existing rows.
        student = self.get(student_id)
        if student.id is not None:
            self.repo.students.update(student.id, {"photo_path": stored_name})
        if actor is not None:
            self.repo.audit.log(action="student.photo_updated", user_id=actor.id,
                                username=actor.username, entity_type="student",
                                entity_id=str(student.id), details={"student_id": student_id})
        logger.info("stored photo for student %s (%d bytes)", student_id, len(content))
        return stored_name

    def photo_path(self, student_id: str) -> Path:
        student = self.get(student_id)
        if not student.photo_path:
            raise NotFoundError("This student has no photo on file")
        # Accept both a bare file name and a repo-relative path, so rows written
        # by an older version keep working.
        raw = student.photo_path
        candidate = Path(raw)
        if not candidate.is_absolute() and str(candidate).startswith(
            self.config.photo_dir.name + "/"
        ):
            path = self.config.photo_dir / candidate.name
        else:
            path = self.config.photo_dir / candidate.name
        if not ensure_within(self.config.photo_dir, path) or not path.is_file():
            raise NotFoundError("The stored photo is missing")
        return path

    def _validate_photo_path(self, photo_path: str | None) -> None:
        if not photo_path:
            return
        if ".." in photo_path or photo_path.startswith("/") or "/" in photo_path:
            raise ValidationError(
                "photo_path must be a bare file name inside the configured upload directory"
            )


_student_service: StudentService | None = None


def get_student_service() -> StudentService:
    global _student_service
    if _student_service is None:
        _student_service = StudentService()
    return _student_service
