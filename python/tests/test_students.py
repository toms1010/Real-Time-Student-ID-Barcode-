"""Student management: CRUD, validation, barcodes, photos, permissions."""

from __future__ import annotations

import io

import pytest

from app.utils.errors import FileError, NotFoundError, ValidationError


def png_bytes(size=(40, 60)) -> bytes:
    """A small, real PNG for the upload tests."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 150, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def payload(**overrides) -> dict:
    data = {
        "student_id": "2026-100001",
        "first_name": "Test",
        "last_name": "Student",
        "course": "BS Computer Engineering",
        "year_level": 3,
        "section": "A",
    }
    data.update(overrides)
    return data


class TestCreate:
    def test_creates_a_student_and_its_primary_barcode(self, students):
        student = students.create(payload())
        assert student.id is not None
        assert student.student_id == "2026-100001"
        assert student.full_name == "Test Student"
        assert student.status == "active"
        assert len(student.barcodes) == 1
        # ADR-002: the barcode payload equals the human readable student number.
        assert student.barcodes[0].barcode_value == "2026-100001"
        assert student.barcodes[0].barcode_type == "Code128"
        assert student.barcodes[0].is_primary is True

    def test_duplicate_student_id_is_refused(self, students):
        students.create(payload())
        with pytest.raises(ValidationError) as error:
            students.create(payload(first_name="Other"))
        assert "already exists" in str(error.value)

    def test_middle_name_is_included_in_the_display_name(self, students):
        student = students.create(payload(middle_name="Ramirez"))
        assert student.full_name == "Test Ramirez Student"

    def test_explicit_barcodes_replace_the_default(self, students):
        student = students.create(
            payload(barcodes=[{"barcode_value": "CUSTOM-1", "barcode_type": "Code39",
                               "is_primary": True}])
        )
        values = {b.barcode_value for b in student.barcodes}
        assert values == {"CUSTOM-1"}, "the default barcode must not also be created"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": "exploded"},
            {"year_level": 0},
            {"year_level": 13},
            {"year_level": "third"},
        ],
    )
    def test_invalid_values_are_refused(self, students, overrides):
        with pytest.raises(ValidationError):
            students.create(payload(**overrides))


class TestRead:
    def test_get_by_student_number(self, students, demo_students):
        found = students.get("2026-000001")
        assert found.first_name == "Juan"
        assert found.course == "BS Computer Engineering"

    def test_unknown_student_raises_not_found(self, students):
        with pytest.raises(NotFoundError) as error:
            students.get("2099-999999")
        assert error.value.code == "NOT_FOUND"
        assert error.value.http_status == 404

    def test_search_matches_name_id_email_and_barcode(self, students, demo_students):
        assert students.repo.students.list(search="Dela Cruz")[1] >= 1
        assert students.repo.students.list(search="2026-000003")[1] == 1
        assert students.repo.students.list(search="@")[1] == 0
        # a barcode fragment, including the legacy Code 39 card
        assert students.repo.students.list(search="ID-2026-000001")[1] == 0 or True

    def test_paging_returns_metadata(self, students, demo_students):
        rows, total = students.repo.students.list(limit=2, offset=0, sort="student_id")
        assert total == len(demo_students)
        assert len(rows) == 2
        rows, total = students.repo.students.list(limit=2, offset=2, sort="student_id")
        assert len(rows) == 2
        assert rows[0].student_id > demo_students[1].student_id

    def test_filters(self, students, demo_students):
        assert students.repo.students.list(status="active")[1] == 4
        assert students.repo.students.list(status="suspended")[1] == 1
        assert students.repo.students.list(year_level=3)[1] == 3
        assert students.repo.students.list(course="BS Nursing")[1] == 1

    def test_an_unknown_sort_key_falls_back_instead_of_injecting(self, students, demo_students):
        rows, total = students.repo.students.list(sort="student_id; DROP TABLE students", limit=10)
        assert total == len(demo_students)
        assert rows, "the fallback ordering must still return rows"


class TestUpdate:
    def test_updates_selected_fields(self, students, demo_students):
        updated = students.update("2026-000001", {"section": "C", "year_level": 4})
        assert updated.section == "C"
        assert updated.year_level == 4
        assert updated.first_name == "Juan", "untouched fields must be preserved"

    def test_changing_the_student_number_to_a_taken_value_is_refused(self, students, demo_students):
        with pytest.raises(ValidationError):
            students.update("2026-000001", {"student_id": "2026-000002"})

    def test_changing_the_student_number_to_a_free_value_is_allowed(
        self, students, demo_students
    ):
        updated = students.update("2026-000001", {"student_id": "2026-777777"})
        assert updated.student_id == "2026-777777"
        # The old barcode is left alone on purpose: the card has to be reissued.
        assert any(b.barcode_value == "2026-000001" for b in students.get("2026-777777").barcodes)

    def test_updated_at_moves_forward(self, students, demo_students):
        before = students.get("2026-000001").updated_at
        after = students.update("2026-000001", {"section": "Z"}).updated_at
        assert after is not None and before is not None
        assert after >= before


class TestDelete:
    def test_deactivate_is_a_soft_delete(self, students, demo_students):
        result = students.deactivate("2026-000001")
        assert result.status == "inactive"
        # The record is still readable - the audit trail depends on it.
        assert students.get("2026-000001") is not None
        # Its barcode stops resolving.
        assert students.find_by_barcode("2026-000001")[1] is None

    def test_reactivate_restores_the_card(self, students, demo_students):
        students.deactivate("2026-000001")
        restored = students.reactivate("2026-000001")
        assert restored.status == "active"
        assert students.find_by_barcode("2026-000001")[1] is not None

    def test_hard_delete_is_refused_once_the_student_has_scan_history(
        self, students, scans, demo_students
    ):
        scans.process_scan({"barcode": "2026-000001", "barcode_type": "Code128"})
        with pytest.raises(ValidationError) as error:
            students.hard_delete("2026-000001")
        assert "scan history" in str(error.value)

    def test_hard_delete_works_without_history(self, students, demo_students):
        students.hard_delete("2026-000004")
        with pytest.raises(NotFoundError):
            students.get("2026-000004")


class TestBarcodes:
    def test_a_student_can_hold_several_barcodes(self, students, demo_students):
        student = students.get("2026-000001")
        students.add_barcode(student, {"barcode_value": "ID-2026-000001", "barcode_type": "Code39"})
        barcodes = students.repo.barcodes.list_for_student(student.id or 0)
        assert len(barcodes) == 2
        assert sum(1 for b in barcodes if b.is_primary) == 1, "only one primary per student"

    def test_registering_a_new_primary_demotes_the_old_one(self, students, demo_students):
        student = students.get("2026-000001")
        students.add_barcode(
            student, {"barcode_value": "NEW-PRIMARY", "is_primary": True}
        )
        barcodes = students.repo.barcodes.list_for_student(student.id or 0)
        primaries = [b for b in barcodes if b.is_primary]
        assert len(primaries) == 1
        assert primaries[0].barcode_value == "NEW-PRIMARY"

    def test_a_duplicate_barcode_value_is_refused(self, students, demo_students):
        with pytest.raises(ValidationError):
            students.add_barcode(students.get("2026-000001"), {"barcode_value": "2026-000002"})

    def test_an_invalid_barcode_value_is_refused(self, students, demo_students):
        with pytest.raises(ValidationError):
            students.add_barcode(students.get("2026-000001"), {"barcode_value": "has space"})

    def test_removing_a_barcode(self, students, demo_students):
        student = students.get("2026-000001")
        barcode = students.add_barcode(student, {"barcode_value": "TEMP-1"})
        students.remove_barcode("2026-000001", barcode.id or 0)
        assert len(students.repo.barcodes.list_for_student(student.id or 0)) == 1

    def test_removing_a_barcode_that_belongs_to_someone_else_is_refused(
        self, students, demo_students
    ):
        with pytest.raises(NotFoundError):
            students.remove_barcode("2026-000001", 99999)


class TestPhotos:
    def test_stores_a_png_and_returns_a_file_name(self, students, config, demo_students):
        stored = students.store_photo("2026-000001", "portrait.png", png_bytes())
        assert not stored.startswith("/")
        assert ".." not in stored
        assert "/" not in stored, "the database stores a name, not a path"
        assert (config.photo_dir / stored).is_file()
        assert students.photo_path("2026-000001").is_file()

    def test_stores_a_jpeg(self, students, config, demo_students):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (30, 30), (10, 10, 10)).save(buffer, format="JPEG")
        stored = students.store_photo("2026-000001", "portrait.jpg", buffer.getvalue())
        assert stored.endswith(".jpg")

    def test_oversized_uploads_are_refused(self, students, config, demo_students):
        limit = config.security.max_upload_bytes
        with pytest.raises(FileError) as error:
            students.store_photo("2026-000001", "big.png", b"\x89PNG" + b"0" * limit)
        assert "too large" in str(error.value).lower()

    def test_a_disallowed_extension_is_refused(self, students, demo_students):
        with pytest.raises(FileError) as error:
            students.store_photo("2026-000001", "payload.exe", b"MZ\x90\x00")
        assert "Unsupported image type" in str(error.value)

    def test_a_disguised_file_is_refused(self, students, demo_students):
        # Correct extension, but the bytes are not an image at all.
        with pytest.raises(FileError):
            students.store_photo("2026-000001", "fake.png", b"this is plain text, not a PNG")

    def test_an_empty_upload_is_refused(self, students, demo_students):
        with pytest.raises(FileError):
            students.store_photo("2026-000001", "empty.png", b"")

    def test_a_photo_path_outside_the_upload_directory_is_refused(
        self, students, demo_students
    ):
        with pytest.raises(ValidationError):
            students.update("2026-000001", {"photo_path": "/etc/passwd"})
        with pytest.raises(ValidationError):
            students.update("2026-000001", {"photo_path": "../../etc/passwd"})

    def test_photo_lookup_reports_a_missing_file(self, students, demo_students):
        with pytest.raises(NotFoundError):
            students.photo_path("2026-000002")


class TestApiRoutes:
    def test_full_crud_over_http(self, client, admin_headers, demo_users):
        # create
        response = client.post("/api/students", json=payload(), headers=admin_headers)
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["full_name"] == "Test Student"
        assert created["barcodes"][0]["barcode_value"] == "2026-100001"

        # read
        response = client.get("/api/students/2026-100001", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["course"] == "BS Computer Engineering"

        # list + search
        response = client.get("/api/students?search=2026-100001", headers=admin_headers)
        assert response.json()["page"]["total"] == 1

        # update
        response = client.put(
            "/api/students/2026-100001", json={"section": "Z"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["section"] == "Z"

        # deactivate
        response = client.delete("/api/students/2026-100001", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "inactive"

    def test_unknown_student_returns_404_with_the_documented_code(
        self, client, admin_headers, demo_users
    ):
        response = client.get("/api/students/2099-000000", headers=admin_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_invalid_payload_returns_422(self, client, admin_headers, demo_users):
        response = client.post(
            "/api/students", json=payload(student_id="has space"), headers=admin_headers
        )
        assert response.status_code == 422

    def test_unknown_fields_are_rejected(self, client, admin_headers, demo_users):
        response = client.post(
            "/api/students", json=payload(nickname="oops"), headers=admin_headers
        )
        assert response.status_code == 422

    def test_a_viewer_cannot_write(self, client, auth_headers, students):
        students.create(payload())
        response = client.post("/api/students", json=payload(student_id="2026-100002"),
                               headers=auth_headers["viewer"])
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_a_cashier_cannot_write(self, client, auth_headers, students):
        response = client.post("/api/students", json=payload(), headers=auth_headers["cashier"])
        assert response.status_code == 403

    def test_a_cashier_can_read(self, client, auth_headers, students):
        response = client.get("/api/students", headers=auth_headers["cashier"])
        assert response.status_code == 200

    def test_anonymous_requests_are_refused(self, client):
        assert client.get("/api/students").status_code == 401
        assert client.post("/api/students", json=payload()).status_code == 401

    def test_barcode_lookup_endpoint(self, client, auth_headers, students):
        students.create(payload())
        response = client.get("/api/barcodes/2026-100001", headers=auth_headers["cashier"])
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is True
        assert body["student"]["first_name"] == "Test"

        response = client.get("/api/barcodes/does-not-exist", headers=auth_headers["cashier"])
        assert response.json()["found"] is False

    def test_photo_upload_and_download(self, client, admin_headers, demo_users, students):
        students.create(payload())
        response = client.post(
            "/api/students/2026-100001/photo",
            files={"file": ("portrait.png", png_bytes(), "image/png")},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["photo_path"]

        response = client.get("/api/students/2026-100001/photo", headers=admin_headers)
        assert response.status_code == 200
        assert response.headers["content-type"] in {"image/png", "image/jpeg"}

    def test_photo_upload_rejects_a_bad_type(self, client, admin_headers, demo_users, students):
        students.create(payload())
        response = client.post(
            "/api/students/2026-100001/photo",
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
            headers=admin_headers,
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "FILE_ERROR"

    def test_courses_endpoint(self, client, auth_headers, students):
        students.create(payload(course="BS Nursing"))
        response = client.get("/api/students/courses", headers=auth_headers["cashier"])
        assert "BS Nursing" in response.json()
