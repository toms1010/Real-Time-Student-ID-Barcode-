"""Tests for configuration loading and time helpers.

These run without a database, so they are the fastest signal that a
configuration change did not break start-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.config import PROJECT_ROOT, load_config
from app.utils.errors import ErrorCode, ValidationError
from app.utils.security import (
    generate_session_token,
    hash_password,
    hash_token,
    safe_filename,
    validate_barcode_payload,
    validate_password_strength,
    validate_username,
    verify_password,
)
from app.utils.timeutil import (
    day_bounds,
    elapsed_ms,
    format_iso,
    parse_iso,
    start_of_day,
    utc_now,
    utc_now_iso,
)


class TestDefaults:
    def test_defaults_match_the_shipped_configuration(self, config):
        assert config.scanner.cooldown_ms == 2000
        assert config.scanner.confidence_threshold == pytest.approx(0.80)
        assert config.api.host == "127.0.0.1"
        assert config.api.port == 8000
        assert config.camera.width == 1280
        assert config.camera.height == 720
        assert config.preprocessing.target_width == 960
        assert config.security.max_upload_bytes == 2 * 1024 * 1024

    def test_the_example_configuration_is_used_when_no_local_file_exists(self):
        # config/config.yaml is git-ignored; the example file is the fallback.
        assert (PROJECT_ROOT / "config" / "config.example.yaml").is_file()
        loaded = load_config(env_file=None)
        assert loaded.config_file is not None
        assert loaded.config_file.name in {"config.yaml", "config.example.yaml"}

    def test_relative_paths_resolve_against_the_project_root(self, config):
        resolved = config.resolve("database/student_id_system.db")
        assert resolved.is_absolute()
        assert str(resolved).startswith(str(PROJECT_ROOT))


class TestValidation:
    @pytest.mark.parametrize(
        "overrides, message",
        [
            ({"camera.width": 0}, "camera.width"),
            ({"camera.roi_x": 0.9, "camera.roi_w": 0.5}, "roi"),
            ({"scanner.cooldown_ms": -1}, "cooldown"),
            ({"scanner.confidence_threshold": 1.5}, "confidence"),
            ({"api.port": 0}, "api.port"),
            ({"audio.volume": 4.0}, "volume"),
            ({"scanner.stream_source": "carrier-pigeon"}, "stream_source"),
        ],
    )
    def test_invalid_values_are_rejected_with_a_clear_message(self, overrides, message):
        with pytest.raises(ValidationError) as error:
            load_config(env_file=None, overrides=overrides)
        assert message in str(error.value)

    def test_valid_overrides_are_applied(self):
        config = load_config(env_file=None, overrides={"scanner.cooldown_ms": 500})
        assert config.scanner.cooldown_ms == 500

    def test_unknown_keys_are_ignored(self):
        config = load_config(env_file=None, overrides={"scanner.no_such_key": 1})
        assert not hasattr(config.scanner, "no_such_key")


class TestTime:
    def test_iso_round_trip(self):
        now = utc_now()
        text = format_iso(now)
        assert text is not None and text.endswith("Z")
        assert parse_iso(text) is not None
        assert abs((parse_iso(text) - now).total_seconds()) < 0.01  # type: ignore[operator]

    def test_parse_accepts_the_z_suffix_and_offsets(self):
        assert parse_iso("2026-09-30T09:42:17Z") is not None
        assert parse_iso("2026-09-30T09:42:17+08:00") is not None
        assert parse_iso("2026-09-30 09:42:17") is not None

    def test_parse_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_iso("not-a-timestamp")

    def test_parse_handles_empty(self):
        assert parse_iso(None) is None
        assert parse_iso("") is None

    def test_stored_format_is_millisecond_utc(self):
        stamp = utc_now_iso()
        assert len(stamp) == 24  # YYYY-MM-DDTHH:MM:SS.mmmZ
        assert stamp[10] == "T" and stamp[23] == "Z"

    def test_day_bounds_are_half_open(self):
        start, end = day_bounds("2026-09-30")
        assert start.startswith("2026-09-30T00:00")
        assert end.startswith("2026-10-01T00:00")
        assert start_of_day("2026-09-30T13:00:00Z").startswith("2026-09-30T00:00")

    def test_elapsed_is_non_negative(self):
        from datetime import timedelta

        start = utc_now()
        assert elapsed_ms(start) >= 0.0
        assert elapsed_ms(start, start) == 0.0
        assert elapsed_ms(start, start + timedelta(milliseconds=250)) == pytest.approx(250.0)


class TestPasswords:
    def test_hash_and_verify(self):
        digest = hash_password("Str0ngPassw0rd")
        assert digest.startswith("$argon2id$")
        assert "Str0ngPassw0rd" not in digest
        assert verify_password(digest, "Str0ngPassw0rd")
        assert not verify_password(digest, "wrong-password")

    def test_hashes_are_salted(self):
        first = hash_password("Str0ngPassw0rd")
        second = hash_password("Str0ngPassw0rd")
        assert first != second, "two hashes of the same password must differ"

    @pytest.mark.parametrize(
        "password, reason",
        [
            ("short1A", "too short"),
            ("alllowercase1", "no uppercase"),
            ("ALLUPPERCASE1", "no lowercase"),
            ("NoDigitsHere", "no digit"),
            ("P" * 300 + "a1", "too long"),
        ],
    )
    def test_weak_passwords_are_refused(self, password, reason):
        with pytest.raises(ValidationError):
            validate_password_strength(password)

    def test_verify_never_raises_on_a_broken_hash(self):
        assert not verify_password("not-a-hash", "anything")
        assert not verify_password("", "anything")

    @pytest.mark.parametrize("username", ["ab", "has space", "bad;char", ""])
    def test_invalid_usernames_are_refused(self, username):
        with pytest.raises(ValidationError):
            validate_username(username)

    @pytest.mark.parametrize("username", ["admin", "cashier1", "a.b_c-d"])
    def test_valid_usernames_are_accepted(self, username):
        assert validate_username(username) == username


class TestSessionTokens:
    def test_tokens_are_unique_and_long(self):
        first, second = generate_session_token(), generate_session_token()
        assert first != second
        assert len(first) >= 40

    def test_only_the_hash_is_stored(self):
        token = generate_session_token()
        digest = hash_token(token)
        assert digest != token
        assert len(digest) == 64  # SHA-256 hex
        assert hash_token(token) == digest


class TestBarcodeValidation:
    PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$"

    @pytest.mark.parametrize(
        "payload", ["2026-000001", "ID-2026-000001", "ABC", "a1_b2"]
    )
    def test_valid_payloads_pass(self, payload):
        assert validate_barcode_payload(payload, self.PATTERN) == payload

    @pytest.mark.parametrize(
        "payload",
        ["", "   ", "has space", "-leading", "a", "x" * 200, "tab\there", "null\x00byte",
         "'; DROP TABLE students; --", "quote'd"],
    )
    def test_invalid_payloads_are_refused(self, payload):
        with pytest.raises(ValidationError):
            validate_barcode_payload(payload, self.PATTERN)

    def test_sql_injection_attempt_is_rejected_before_any_query(self):
        # The pattern rejects quotes and spaces, so the string can never become
        # part of a SQL statement.
        with pytest.raises(ValidationError):
            validate_barcode_payload("' OR '1'='1", self.PATTERN)

    def test_control_characters_are_rejected(self):
        with pytest.raises(ValidationError):
            validate_barcode_payload("2026\n000001", self.PATTERN)


class TestFileHelpers:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("photo.jpg", "photo.jpg"),
            ("../../etc/passwd", ".._.._etc_passwd"),
            ("my photo (1).png", "my_photo__1_.png"),
            ("", "file"),
        ],
    )
    def test_safe_filename_neutralises_traversal(self, name, expected):
        assert safe_filename(name) == expected

    def test_ensure_within_rejects_escapes(self, tmp_path: Path):
        from app.utils.security import ensure_within

        assert ensure_within(tmp_path, tmp_path / "ok.txt")
        assert not ensure_within(tmp_path, tmp_path / ".." / "outside.txt")


class TestErrorCatalogue:
    def test_every_documented_error_code_has_a_message_and_status(self):
        from app.utils.errors import ErrorCode

        documented = {
            "CAMERA_NOT_FOUND", "CAMERA_PERMISSION_DENIED", "CAMERA_DISCONNECTED",
            "FRAME_CAPTURE_FAILED", "BARCODE_NOT_DETECTED", "BARCODE_DECODE_FAILED",
            "INVALID_BARCODE", "STUDENT_NOT_FOUND", "DATABASE_ERROR", "API_UNAVAILABLE",
            "FILE_ERROR", "UNKNOWN_ERROR",
        }
        available = {str(code) for code in ErrorCode}
        assert documented <= available, f"missing codes: {documented - available}"
        for code in ErrorCode:
            assert code.user_message
            assert 400 <= code.http_status <= 599

    def test_app_error_serialises_to_the_documented_envelope(self):
        from app.utils.errors import AppError

        error = AppError(ErrorCode.STUDENT_NOT_FOUND, details={"student_id": "2026-000001"})
        payload = error.to_dict()
        assert payload["success"] is False
        assert payload["error"]["code"] == "STUDENT_NOT_FOUND"
        assert payload["error"]["details"]["student_id"] == "2026-000001"
        assert error.http_status == 404
