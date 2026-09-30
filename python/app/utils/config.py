"""Application configuration.

Precedence (lowest to highest)::

    dataclass defaults  <  config/config.yaml  <  environment variables  <  .env

The same YAML file is read by the C++ scanner (``cpp/src/utils/Config.cpp``), so
there is exactly one file to edit when moving a deployment to another machine.

Environment variable names are listed in ``.env.example``.  Nothing secret has a
usable default: :attr:`AppConfig.session_secret` must come from the environment
when authentication is required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from app.utils.errors import ValidationError
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Repository root: python/app/utils/config.py -> python/app/utils -> ... -> root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "config.example.yaml"


# ---------------------------------------------------------------------------
# Section dataclasses - these mirror config/config.example.yaml one to one.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ApplicationConfig:
    name: str = "Student ID Barcode Detection System"
    environment: str = "development"  # development | testing | production
    version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: str = "text"  # text | json
    log_file: str | None = None


@dataclass(slots=True)
class CameraConfig:
    device_index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    source: str = ""  # blank = device index, else file path or rtsp:// URL
    warmup_frames: int = 5
    frame_timeout_ms: int = 2000
    # Fractional region of interest (0..1) used for cropping and the overlay.
    roi_x: float = 0.10
    roi_y: float = 0.25
    roi_w: float = 0.80
    roi_h: float = 0.50
    flip: bool = False  # for front-facing / mirrored webcams


@dataclass(slots=True)
class ScannerConfig:
    cooldown_ms: int = 2000
    confidence_threshold: float = 0.80
    barcode_timeout_ms: int = 3000
    detect_roi: bool = True
    max_tracked_barcodes: int = 512
    decode_attempts: int = 2  # original frame + one preprocessed variant
    allowed_formats: tuple[str, ...] = (
        "Code128",
        "Code39",
        "EAN13",
        "EAN8",
        "UPCA",
        "UPCE",
        "ITF",
        "QRCode",
        "DataMatrix",
    )
    payload_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$"
    # Where frames come from: "python" (FastAPI owns the device - default),
    # "cpp" (student_id_scanner --mode=service owns it) or "file" (replay).
    stream_source: str = "python"
    file_source: str = "tests/sample_images/scan_sequence.mp4"
    duplicate_log_per_cooldown: bool = True


@dataclass(slots=True)
class PreprocessingConfig:
    enable_grayscale: bool = True
    enable_roi_crop: bool = True
    target_width: int = 960
    enable_clahe: bool = True
    clahe_clip_limit: float = 2.0
    clahe_dark_trigger: float = 0.45  # only apply CLAHE below this mean luminance
    enable_denoise: bool = True
    denoise_h: float = 5.0
    denoise_trigger: float = 0.02  # Laplacian variance threshold
    enable_adaptive_threshold: bool = False  # ADR-007: off by default
    adaptive_block_size: int = 31
    adaptive_c: float = 5.0
    adaptive_bimodal_threshold: float = 0.55
    enable_sharpen: bool = False
    sharpen_amount: float = 0.6


@dataclass(slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    timeout_ms: int = 3000
    retries: int = 2
    retry_backoff_ms: int = 120
    cors_origins: tuple[str, ...] = ()


@dataclass(slots=True)
class DatabaseConfig:
    path: str = "database/student_id_system.db"
    busy_timeout_ms: int = 5000
    enable_wal: bool = True


@dataclass(slots=True)
class AudioConfig:
    enabled: bool = True
    volume: float = 0.8
    player: str = "auto"  # auto | aplay | paplay | ffplay | none
    assets_dir: str = "assets/sounds"


@dataclass(slots=True)
class SecurityConfig:
    require_login: bool = True
    session_ttl_minutes: int = 720
    session_secret: str = ""
    max_login_attempts: int = 5
    login_lockout_seconds: int = 60
    max_upload_bytes: int = 2 * 1024 * 1024
    allowed_photo_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png")
    cookie_name: str = "sidb_session"
    cookie_secure: bool = False  # set true only when served over HTTPS


@dataclass(slots=True)
class UploadsConfig:
    photo_dir: str = "uploads/photos"
    max_photo_dimension: int = 1600


@dataclass(slots=True)
class ReportsConfig:
    output_dir: str = "exports"
    timezone: str = "Asia/Manila"
    default_page_size: int = 50
    max_page_size: int = 500


@dataclass(slots=True)
class WebConfig:
    dist_dir: str = "frontend/dist"
    mount_frontend: bool = True


@dataclass(slots=True)
class AppConfig:
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    preprocessing: PreprocessingConfig = field(default_factory=PreprocessingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    uploads: UploadsConfig = field(default_factory=UploadsConfig)
    reports: ReportsConfig = field(default_factory=ReportsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    config_file: Path | None = None

    def validate(self) -> None:
        """Fail fast on nonsense configuration instead of at scan time."""
        if self.camera.width <= 0 or self.camera.height <= 0:
            raise ValidationError("camera.width and camera.height must be positive")
        if not (0.0 <= self.camera.roi_x and self.camera.roi_x + self.camera.roi_w <= 1.0):
            raise ValidationError("camera.roi_x/roi_w must stay inside the frame")
        if not (0.0 <= self.camera.roi_y and self.camera.roi_y + self.camera.roi_h <= 1.0):
            raise ValidationError("camera.roi_y/roi_h must stay inside the frame")
        if self.scanner.cooldown_ms < 0:
            raise ValidationError("scanner.cooldown_ms must be >= 0")
        if not (0.0 <= self.scanner.confidence_threshold <= 1.0):
            raise ValidationError("scanner.confidence_threshold must be between 0 and 1")
        if not self.api.host:
            raise ValidationError("api.host must not be empty")
        if not (1 <= self.api.port <= 65535):
            raise ValidationError("api.port must be between 1 and 65535")
        if not (0.0 <= self.audio.volume <= 1.0):
            raise ValidationError("audio.volume must be between 0 and 1")
        if self.security.max_upload_bytes <= 0:
            raise ValidationError("security.max_upload_bytes must be positive")
        if self.scanner.stream_source not in {"python", "cpp", "file"}:
            raise ValidationError("scanner.stream_source must be one of: python, cpp, file")

    # -- path helpers -------------------------------------------------------
    def resolve(self, value: str | os.PathLike[str]) -> Path:
        """Resolve a config path relative to the project root."""
        path = Path(value)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.database.path)

    @property
    def schema_path(self) -> Path:
        return self.resolve("database/schema.sql")

    @property
    def seed_path(self) -> Path:
        return self.resolve("database/seed.sql")

    @property
    def migrations_dir(self) -> Path:
        return self.resolve("database/migrations")

    @property
    def photo_dir(self) -> Path:
        return self.resolve(self.uploads.photo_dir)

    @property
    def frontend_dist(self) -> Path:
        return self.resolve(self.web.dist_dir)

    @property
    def report_dir(self) -> Path:
        return self.resolve(self.reports.output_dir)

    @property
    def sound_dir(self) -> Path:
        return self.resolve(self.audio.assets_dir)

    @property
    def api_base_url(self) -> str:
        return f"http://{self.api.host}:{self.api.port}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _coerce(value: Any, target_type: Any, *, path: str) -> Any:
    """Convert a YAML/JSON scalar to the annotated dataclass field type."""
    if value is None:
        return None
    origin = getattr(target_type, "__origin__", None)
    if origin is tuple:
        args = getattr(target_type, "__args__", (str,))
        if isinstance(value, str):
            items: Any = [v.strip() for v in value.split(",") if v.strip()]
        else:
            items = list(value)
        if args and args[0] is bool:
            return tuple(str(v).strip().lower() in {"1", "true", "yes", "on"} for v in items)
        return tuple(str(v) for v in items)
    if target_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if target_type is int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{path} must be an integer") from exc
    if target_type is float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{path} must be a number") from exc
    if target_type is str:
        return str(value)
    return value


_TYPE_HINTS: dict[type, dict[str, Any]] = {}


def _field_types(section: Any) -> dict[str, Any]:
    """Resolve a dataclass' (stringified) annotations to real types once."""
    cls = type(section)
    if cls not in _TYPE_HINTS:
        from typing import get_type_hints

        _TYPE_HINTS[cls] = get_type_hints(cls)
    return _TYPE_HINTS[cls]


def _apply_section(section: Any, mapping: dict[str, Any], *, prefix: str) -> None:
    valid = {f.name for f in fields(section)}
    hints = _field_types(section)
    for key, value in (mapping or {}).items():
        if key not in valid:
            logger.warning("ignoring unknown configuration key: %s%s", prefix, key)
            continue
        try:
            setattr(section, key, _coerce(value, hints[key], path=f"{prefix}{key}"))
        except ValidationError as exc:
            logger.error("invalid configuration: %s", exc)


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (no python-dotenv dependency).

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments and quoted
    values.  Existing environment variables always win.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            values[key] = value
    if values:
        os.environ.update(values)
    return values


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ValidationError(
            "PyYAML is required to read the YAML configuration. "
            "Install it with: pip install -r requirements.txt"
        ) from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ValidationError(f"could not parse {path.name}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError(f"{path.name} must contain a mapping at the top level")
    return data


#: Environment variable -> (section, field, kind)
_ENV_MAP: dict[str, tuple[str, str, str]] = {
    "APP_NAME": ("application", "name", "str"),
    "APP_ENV": ("application", "environment", "str"),
    "LOG_LEVEL": ("application", "log_level", "str"),
    "LOG_FORMAT": ("application", "log_format", "str"),
    "API_HOST": ("api", "host", "str"),
    "API_PORT": ("api", "port", "int"),
    "CORS_ORIGINS": ("api", "cors_origins", "tuple"),
    "DATABASE_URL": ("database", "path", "str"),
    "DB_BUSY_TIMEOUT_MS": ("database", "busy_timeout_ms", "int"),
    "DB_ENABLE_WAL": ("database", "enable_wal", "bool"),
    "CAMERA_DEVICE_INDEX": ("camera", "device_index", "int"),
    "CAMERA_FRAME_WIDTH": ("camera", "width", "int"),
    "CAMERA_FRAME_HEIGHT": ("camera", "height", "int"),
    "CAMERA_FPS": ("camera", "fps", "int"),
    "CAMERA_SOURCE": ("camera", "source", "str"),
    "SCAN_COOLDOWN_MS": ("scanner", "cooldown_ms", "int"),
    "CONFIDENCE_THRESHOLD": ("scanner", "confidence_threshold", "float"),
    "BARCODE_TIMEOUT_MS": ("scanner", "barcode_timeout_ms", "int"),
    "DETECT_ROI": ("scanner", "detect_roi", "bool"),
    "ALLOWED_BARCODE_FORMATS": ("scanner", "allowed_formats", "tuple"),
    "BARCODE_PATTERN": ("scanner", "payload_pattern", "str"),
    "AUDIO_ENABLED": ("audio", "enabled", "bool"),
    "AUDIO_VOLUME": ("audio", "volume", "float"),
    "AUDIO_PLAYER": ("audio", "player", "str"),
    "SESSION_SECRET": ("security", "session_secret", "str"),
    "SESSION_TTL_MINUTES": ("security", "session_ttl_minutes", "int"),
    "MAX_LOGIN_ATTEMPTS": ("security", "max_login_attempts", "int"),
    "LOGIN_LOCKOUT_SECONDS": ("security", "login_lockout_seconds", "int"),
    "MAX_UPLOAD_BYTES": ("security", "max_upload_bytes", "int"),
    "ALLOWED_PHOTO_EXTENSIONS": ("security", "allowed_photo_extensions", "tuple"),
    "REPORT_TIMEZONE": ("reports", "timezone", "str"),
    "REPORT_OUTPUT_DIR": ("reports", "output_dir", "str"),
}


def _database_path_from_url(url: str) -> str:
    """Accept both ``DATABASE_URL=sqlite:///path`` and a bare path."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    if url.startswith("sqlite://"):
        return url[len("sqlite://") :]
    return url


def load_config(
    config_file: str | os.PathLike[str] | None = None,
    *,
    env_file: str | os.PathLike[str] | None = ".env",
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from YAML + environment.

    ``overrides`` accepts ``{"scanner.cooldown_ms": 500}`` style dotted keys and
    is applied last; tests use it to build isolated configurations.
    """
    config = AppConfig()

    # 1. environment (.env first, so APP_ENV etc. are available for logging)
    if env_file:
        _load_dotenv(PROJECT_ROOT / str(env_file) if not os.path.isabs(str(env_file))
                     else Path(str(env_file)))

    # 2. YAML
    path = Path(config_file) if config_file else None
    if path is None:
        candidate = DEFAULT_CONFIG_FILE
        path = candidate if candidate.is_file() else (
            DEFAULT_EXAMPLE_CONFIG if DEFAULT_EXAMPLE_CONFIG.is_file() else None
        )
    if path is not None and path.is_file():
        data = load_yaml(path)
        config.config_file = path
        for section_name, section_value in data.items():
            section = getattr(config, section_name, None)
            if section is None or not is_dataclass(section):
                logger.warning("ignoring unknown configuration section: %s", section_name)
                continue
            if isinstance(section_value, dict):
                _apply_section(section, section_value, prefix=f"{section_name}.")

    # 3. environment overrides
    for env_key, (section_name, attr, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        target = {"str": str, "int": int, "float": float, "bool": bool, "tuple": tuple}[kind]
        value = _coerce(raw, target, path=env_key)
        setattr(getattr(config, section_name), attr, value)
    if os.environ.get("DATABASE_URL"):
        config.database.path = _database_path_from_url(os.environ["DATABASE_URL"])

    # 4. explicit overrides
    for dotted, value in (overrides or {}).items():
        section_name, _, attr = dotted.partition(".")
        section = getattr(config, section_name, None)
        if section is None or not hasattr(section, attr):
            logger.warning("ignoring unknown configuration override: %s", dotted)
            continue
        setattr(section, attr, value)

    config.validate()
    return config


_CACHED: AppConfig | None = None


def get_config(reload: bool = False) -> AppConfig:
    """Process-wide configuration singleton."""
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = load_config()
    return _CACHED
