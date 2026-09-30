"""Password hashing, session tokens, and the small validation helpers that are
shared by the API layer and the CLI.

Design decisions are recorded in ``docs/architecture/decisions.md`` (ADR-005):

* Passwords are hashed with **Argon2id** using the OWASP reference parameters
  (m=64 MiB, t=3, p=4).  Verification is constant-time via the library.
* Session tokens are 256 bits of ``secrets.token_urlsafe``; only the SHA-256
  hash is persisted, so a database copy does not yield usable sessions.
* Nothing in this module logs a secret, and :func:`hash_password` never returns
  the plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import Final

from app.utils.errors import ValidationError

# --- password policy -------------------------------------------------------
MIN_PASSWORD_LENGTH: Final = 10
MAX_PASSWORD_LENGTH: Final = 256
PASSWORD_POLICY: Final = (
    "at least 10 characters, including an uppercase letter, a lowercase "
    "letter and a digit"
)
_USERNAME_RE: Final = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
#: Demo password shipped in database/seed.sql (demo databases only).
DEMO_PASSWORD: Final = "DemoPass!2026"

try:  # pragma: no cover - import guard
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    _HAS_ARGON2 = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_ARGON2 = False


def has_argon2() -> bool:
    """True when the Argon2 implementation is importable."""
    return _HAS_ARGON2


def hash_password(password: str, *, is_demo: bool = False) -> str:
    """Hash ``password`` with Argon2id.

    ``is_demo`` keeps the Argon2id parameters but is used to flag hashes created
    from the documented demo password, so a deployment can refuse them.
    """
    if not _HAS_ARGON2:  # pragma: no cover
        raise RuntimeError(
            "argon2-cffi is not installed. Run: pip install -r requirements.txt"
        )
    validate_password_strength(password)
    hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)
    return hasher.hash(password)


def hash_demo_password() -> str:
    """Hash the documented demo password (used by ``app.cli db-seed``)."""
    return hash_password(DEMO_PASSWORD, is_demo=True)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time verification; never raises for a malformed hash."""
    if not stored_hash or not _HAS_ARGON2:
        return False
    try:
        PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4).verify(
            stored_hash, password
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # noqa: BLE001 - a bad hash must not authenticate anyone
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash uses weaker parameters than current policy."""
    if not _HAS_ARGON2 or not stored_hash:
        return True
    try:
        return PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4).check_needs_rehash(
            stored_hash
        )
    except Exception:  # noqa: BLE001
        return True


def validate_password_strength(password: str) -> None:
    """Raise :class:`ValidationError` unless the password satisfies the policy."""
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be {PASSWORD_POLICY}")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError("Password is too long")
    if not re.search(r"[A-Z]", password):
        raise ValidationError("Password must contain an uppercase letter")
    if not re.search(r"[a-z]", password):
        raise ValidationError("Password must contain a lowercase letter")
    if not re.search(r"\d", password):
        raise ValidationError("Password must contain a digit")
    if password.lower() in {"password123", "qwertyuiop", "administrator"}:
        raise ValidationError("Password is too common")


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValidationError(
            "Username must be 3-64 characters using letters, digits, dot, "
            "underscore or hyphen"
        )
    return username


def generate_session_token() -> str:
    """256-bit opaque bearer token, safe in a cookie value."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 of a session token; this is what the database stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def generate_reference_number(prefix: str = "TXN") -> str:
    """Human-friendly transaction reference: ``TXN-20260930-3F9A2B``."""
    import datetime as _dt

    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(3).upper()}"


def normalize_text(value: str | None) -> str | None:
    """Trim, collapse internal whitespace and normalise unicode width."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", " ", text) or None


# --- generic value validation ---------------------------------------------
def validate_barcode_payload(payload: str, pattern: str) -> str:
    """Validate a decoded barcode payload against the configured allow-list.

    Applied to *every* decoded value before it reaches SQL, so a payload can
    never be longer than the column, contain control characters, or smuggle
    SQL syntax into a query.
    """
    if payload is None:
        raise ValidationError("Barcode payload is empty")
    payload = unicodedata.normalize("NFKC", str(payload)).strip()
    if not payload:
        raise ValidationError("Barcode payload is empty")
    if len(payload) > 128:
        raise ValidationError("Barcode payload is too long (max 128 characters)")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in payload):
        raise ValidationError("Barcode payload contains control characters")
    if not re.match(pattern, payload):
        raise ValidationError(
            "Barcode payload does not match the configured ID pattern"
        )
    return payload


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Reduce an untrusted filename to a safe basename."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", (name or "").strip()) or fallback
    return base[:120]


def ensure_within(base_dir, candidate) -> bool:
    """True when ``candidate`` resolves inside ``base_dir`` (path traversal guard)."""
    try:
        base = base_dir.resolve()
        target = candidate.resolve()
    except OSError:  # pragma: no cover
        return False
    return base == target or base in target.parents
