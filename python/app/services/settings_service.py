"""Runtime settings backed by the ``system_settings`` table.

Two configuration sources exist and both are needed:

``config/config.yaml`` + environment
    Deployment-level: bind address, camera geometry, credentials.  Requires a
    restart (or at least a reload) to change.

``system_settings`` table
    Operational values an administrator edits from the Settings screen:
    application name, theme, cooldown, confidence, sound, session TTL.  Read on
    every request, so a change takes effect immediately.

The service keeps a validated view of the table and pushes selected keys into
the in-memory :class:`~app.utils.config.AppConfig` so the vision pipeline and the
C++ config dump see the same values.  Unknown keys and malformed values are
rejected with a clear message rather than silently ignored.
"""

from __future__ import annotations

from typing import Any

from app.database.repository import Repository
from app.schemas.report import SettingsOut
from app.utils.config import AppConfig, get_config
from app.utils.errors import ForbiddenError, ValidationError
from app.utils.logger import get_logger
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)

#: key -> (allowed values or type, "live" settings are pushed into AppConfig)
SETTING_SCHEMA: dict[str, tuple[str, str | None]] = {
    "application_name": ("str", None),
    "theme": ("choice:dark,light,system", None),
    "camera_index": ("int", "camera.device_index"),
    "camera_width": ("int", "camera.width"),
    "camera_height": ("int", "camera.height"),
    "camera_fps": ("int", "camera.fps"),
    "scan_cooldown_ms": ("int", "scanner.cooldown_ms"),
    "barcode_timeout_ms": ("int", "scanner.barcode_timeout_ms"),
    "confidence_threshold": ("float", "scanner.confidence_threshold"),
    "barcode_pattern": ("str", "scanner.payload_pattern"),
    "allowed_barcode_formats": ("str", "scanner.allowed_formats"),
    "sound_enabled": ("bool", "audio.enabled"),
    "sound_volume": ("float", "audio.volume"),
    "success_sound": ("str", None),
    "error_sound": ("str", None),
    "unknown_sound": ("str", None),
    "scan_sound": ("str", None),
    "session_ttl_minutes": ("int", "security.session_ttl_minutes"),
    "require_login": ("bool", "security.require_login"),
}

#: Only an administrator may change these.
ADMIN_ONLY = {"require_login", "session_ttl_minutes", "allowed_barcode_formats",
              "barcode_pattern", "confidence_threshold"}


class SettingsService:
    def __init__(self, repo: Repository | None = None, config: AppConfig | None = None) -> None:
        self.repo = repo or Repository()
        self.config = config or get_config()

    # -- reads -------------------------------------------------------------
    def list(self) -> list[SettingsOut]:
        return [
            SettingsOut(
                key=row["key"],
                value=row["value"],
                value_type=row["value_type"],
                category=row["category"],
                description=row["description"],
                updated_at=row["updated_at"],
            )
            for row in self.repo.settings.typed()
        ]

    def as_dict(self) -> dict[str, Any]:
        """Typed view consumed by the UI (values are coerced, not strings)."""
        typed: dict[str, Any] = {}
        for row in self.repo.settings.typed():
            value: Any = row["value"]
            kind = row["value_type"]
            try:
                if kind == "int":
                    value = int(value)
                elif kind == "float":
                    value = float(value)
                elif kind == "bool":
                    value = str(value).strip().lower() in {"1", "true", "yes", "on"}
            except (TypeError, ValueError):
                logger.warning("setting %s has a non-numeric %s value; ignoring",
                               row["key"], kind)
                continue
            typed[row["key"]] = value
        return typed

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def public_snapshot(self) -> dict[str, Any]:
        """The subset that is safe to send to any authenticated client."""
        return {
            "application_name": self.get("application_name", self.config.application.name),
            "theme": self.get("theme", "dark"),
            "cooldown_ms": self.get("scan_cooldown_ms", self.config.scanner.cooldown_ms),
            "confidence_threshold": self.get(
                "confidence_threshold", self.config.scanner.confidence_threshold
            ),
            "sound_enabled": self.get("sound_enabled", self.config.audio.enabled),
            "sound_volume": self.get("sound_volume", self.config.audio.volume),
            "require_login": self.get("require_login", self.config.security.require_login),
            "camera": {
                "device_index": self.get("camera_index", self.config.camera.device_index),
                "width": self.get("camera_width", self.config.camera.width),
                "height": self.get("camera_height", self.config.camera.height),
                "fps": self.get("camera_fps", self.config.camera.fps),
            },
            "allowed_barcode_formats": list(self.config.scanner.allowed_formats),
            "session_ttl_minutes": self.get(
                "session_ttl_minutes", self.config.security.session_ttl_minutes
            ),
        }

    # -- writes ------------------------------------------------------------
    def update(self, values: dict[str, Any], *, actor: Any = None,
               reason: str | None = None) -> list[SettingsOut]:
        if not values:
            raise ValidationError("no settings supplied")
        normalised: dict[str, str] = {}
        for key, raw in values.items():
            normalised[key] = self._validate(key, raw)
            if actor is not None and key in ADMIN_ONLY and not actor.has("settings:write"):
                raise ForbiddenError(f"only an administrator may change '{key}'")

        changed: list[str] = []
        for key, value in normalised.items():
            previous = self.repo.settings.get(key)
            if previous == value:
                continue
            self.repo.settings.set(key, value, user_id=getattr(actor, "id", None))
            changed.append(key)

        if changed:
            self._apply_to_config(normalised)
            if actor is not None:
                self.repo.audit.log(
                    action="settings.updated", user_id=actor.id, username=actor.username,
                    entity_type="system_settings", details={"keys": changed, "reason": reason},
                )
            logger.info("settings updated by %s: %s",
                        getattr(actor, "username", "system"), ", ".join(sorted(changed)))
        return self.list()

    def _validate(self, key: str, raw: Any) -> str:
        if key not in SETTING_SCHEMA:
            raise ValidationError(f"unknown setting: {key}")
        kind, _ = SETTING_SCHEMA[key]
        if kind.startswith("choice:"):
            allowed = kind.split(":", 1)[1].split(",")
            if str(raw) not in allowed:
                raise ValidationError(f"'{key}' must be one of: {', '.join(allowed)}")
            return str(raw)
        if kind == "int":
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"'{key}' must be an integer") from exc
            if number < 0:
                raise ValidationError(f"'{key}' must not be negative")
            return str(number)
        if kind == "float":
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"'{key}' must be a number") from exc
            if not 0.0 <= number <= 1.0 and key in {"sound_volume", "confidence_threshold"}:
                raise ValidationError(f"'{key}' must be between 0 and 1")
            return str(number)
        if kind == "bool":
            if isinstance(raw, bool):
                return "true" if raw else "false"
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return "true"
            if text in {"0", "false", "no", "off"}:
                return "false"
            raise ValidationError(f"'{key}' must be a boolean")
        text = str(raw)
        if len(text) > 512:
            raise ValidationError(f"'{key}' is too long (max 512 characters)")
        return text

    def _apply_to_config(self, values: dict[str, str]) -> None:
        """Push live settings into the in-process configuration."""
        for key, value in values.items():
            target = SETTING_SCHEMA[key][1]
            if not target:
                continue
            section_name, _, attr = target.partition(".")
            section = getattr(self.config, section_name, None)
            if section is None or not hasattr(section, attr):
                continue
            current = getattr(section, attr)
            try:
                if isinstance(current, bool):
                    setattr(section, attr, value == "true")
                elif isinstance(current, int):
                    setattr(section, attr, int(value))
                elif isinstance(current, float):
                    setattr(section, attr, float(value))
                elif isinstance(current, tuple):
                    setattr(section, attr, tuple(v.strip() for v in value.split(",") if v.strip()))
                else:
                    setattr(section, attr, value)
            except (TypeError, ValueError):  # pragma: no cover - validated above
                logger.warning("could not apply setting %s to %s", key, target)

    def reset(self, keys: list[str], *, actor: Any = None) -> list[SettingsOut]:
        """Remove overrides for the given keys so the YAML/env value applies."""
        for key in keys:
            if key not in SETTING_SCHEMA:
                raise ValidationError(f"unknown setting: {key}")
            self.repo.db.execute("DELETE FROM system_settings WHERE key = ?", (key,))
            if actor is not None:
                self.repo.audit.log(action="settings.reset", user_id=actor.id,
                                    username=actor.username, entity_type="system_settings",
                                    details={"keys": keys})
        return self.list()

    def touch(self) -> None:
        """Record that the settings were read (used by the health endpoint)."""
        self.repo.db.execute(
            "UPDATE system_settings SET updated_at = ? WHERE key = 'application_name'",
            (utc_now_iso(),),
        )


_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    global _settings_service
    if _settings_service is None:
        _settings_service = SettingsService()
    return _settings_service
