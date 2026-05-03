"""HMAC-SHA256 pseudonymization for user identifiers in logs."""

import hashlib
import hmac

_DEV_SALT = b"dev-salt-do-not-use-in-production"


def public_user_ref(telegram_id: str) -> str:
    """Return a stable 12-char HMAC-SHA256 pseudonym of telegram_id for logging.

    Falls back to a fixed dev salt when LOG_PSEUDONYM_SALT is not configured so
    logs remain correlatable in local dev without exposing real IDs.
    """
    # Import deferred to avoid circular imports at module load time
    from shared.config import get_settings

    raw_salt = get_settings().LOG_PSEUDONYM_SALT
    salt = raw_salt.encode() if raw_salt else _DEV_SALT
    return hmac.new(salt, telegram_id.encode(), hashlib.sha256).hexdigest()[:12]
