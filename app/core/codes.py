"""One-time codes for admin invites and password resets.

A code is 8 characters from an alphabet without look-alike characters (no 0/O, 1/I/L). Only an HMAC
of the code, keyed with ``INVITE_CODE_PEPPER`` and bound to the purpose and the record id, is
stored. The plain code and its hash are never logged and never returned by any API.
"""

import hashlib
import hmac
import secrets
from typing import Literal
from uuid import UUID

from fastapi import status

from app.core.config import get_settings
from app.core.errors import AppError

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
Purpose = Literal["invite", "reset"]

# Compared against when no record matches, so an unknown email costs the same HMAC work.
DUMMY_CODE_HASH = "0" * 64
DUMMY_RECORD_ID = "00000000-0000-0000-0000-000000000000"


def codes_unavailable() -> AppError:
    return AppError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "codes_unavailable",
        "Invitations and password resets are not configured on this server.",
    )


def ensure_codes_configured() -> None:
    """Raise 503 ``codes_unavailable`` when ``INVITE_CODE_PEPPER`` is missing."""
    if not get_settings().codes_configured:
        raise codes_unavailable()


def generate_code() -> str:
    """Return a new random 8-character code."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """Show a code as ``XXXX-XXXX`` for an email."""
    normalized = normalize_code(code)
    return f"{normalized[:4]}-{normalized[4:]}"


def normalize_code(code: str) -> str:
    """Uppercase and remove spaces and hyphens, so ``abcd-efgh`` and ``ABCD EFGH`` match."""
    return code.upper().replace(" ", "").replace("-", "")


def hash_code(purpose: Purpose, record_id: UUID | str, code: str) -> str:
    """HMAC-SHA256 of ``purpose:record_id:normalized_code`` keyed with the pepper."""
    pepper = get_settings().invite_code_pepper
    if not pepper:
        raise codes_unavailable()
    message = f"{purpose}:{record_id}:{normalize_code(code)}"
    return hmac.new(pepper.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_code(purpose: Purpose, record_id: UUID | str, code: str, stored_hash: str) -> bool:
    """Compare in constant time; the HMAC is computed even for the dummy hash."""
    return hmac.compare_digest(hash_code(purpose, record_id, code), stored_hash)


def burn_dummy_check(purpose: Purpose, code: str) -> None:
    """Spend the same work as a real check when there is nothing to check against."""
    verify_code(purpose, DUMMY_RECORD_ID, code, DUMMY_CODE_HASH)
