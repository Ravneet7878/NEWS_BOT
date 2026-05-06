"""Async Firestore helper functions — module-level singleton, no wrapper classes."""

from datetime import date, datetime, timedelta, timezone

from google.cloud.firestore_v1 import FieldFilter  # type: ignore[import-untyped]
from google.cloud.firestore_v1.async_client import AsyncClient  # type: ignore[import-untyped]
from google.cloud.firestore_v1.async_transaction import async_transactional  # type: ignore[import-untyped]

from shared.config import settings
from shared.models import InviteCode, OnboardingState, User
from utils.logging import get_logger
from utils.privacy import public_user_ref
from utils.time import utc_now

logger = get_logger(__name__)

# Module-level singleton — one connection pool for the lifetime of the process
_db: AsyncClient = AsyncClient(project=settings.GCP_PROJECT_ID)

_USERS_COL = "users"
_INVITE_CODES_COL = "invite_codes"
_DIGEST_HISTORY_COL = "user_digest_history"
_PENDING_COL = "pending_digests"


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------


async def get_user(telegram_id: str) -> User | None:
    """Return the User for telegram_id, or None if not found."""
    try:
        doc = await _db.collection(_USERS_COL).document(telegram_id).get()
        if not doc.exists:
            return None
        return User(**doc.to_dict())
    except Exception as exc:
        logger.error("get_user(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        raise


async def create_user(user: User) -> None:
    """Persist a new User document to Firestore."""
    try:
        await _db.collection(_USERS_COL).document(user.telegram_id).set(
            user.model_dump(mode="json")
        )
    except Exception as exc:
        logger.error("create_user(%s) failed: %s", public_user_ref(user.telegram_id), exc, exc_info=True)
        raise


async def update_user(telegram_id: str, **fields: object) -> None:
    """Partial-update specific fields on an existing User document."""
    try:
        await _db.collection(_USERS_COL).document(telegram_id).update(fields)
    except Exception as exc:
        logger.error("update_user(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        raise


async def delete_user(telegram_id: str) -> None:
    """Delete a User document from Firestore."""
    try:
        await _db.collection(_USERS_COL).document(telegram_id).delete()
    except Exception as exc:
        logger.error("delete_user(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        raise


async def delete_user_data(telegram_id: str) -> None:
    """Delete the user document plus all owned digest history and pending digest docs."""
    import asyncio

    async def _delete_all(collection: str, field: str, value: str) -> None:
        docs = (
            await _db.collection(collection)
            .where(filter=FieldFilter(field, "==", value))
            .get()
        )
        if docs:
            await asyncio.gather(*[doc.reference.delete() for doc in docs])

    try:
        await asyncio.gather(
            _db.collection(_USERS_COL).document(telegram_id).delete(),
            # pending_digests stores the plain telegram_id
            _delete_all(_PENDING_COL, "telegram_id", telegram_id),
            # user_digest_history stores the pseudonymized user_ref
            _delete_all(_DIGEST_HISTORY_COL, "user_ref", public_user_ref(telegram_id)),
        )
    except Exception as exc:
        logger.error("delete_user_data(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Invite code helpers
# ---------------------------------------------------------------------------


async def get_invite_code(code: str) -> InviteCode | None:
    """Return the InviteCode for code, or None if not found."""
    try:
        doc = await _db.collection(_INVITE_CODES_COL).document(code).get()
        if not doc.exists:
            return None
        return InviteCode(**doc.to_dict())
    except Exception as exc:
        logger.error("get_invite_code(%s) failed: %s", code, exc, exc_info=True)
        raise


async def mark_code_used(code: str, used_by: str) -> None:
    """Mark an invite code as used."""
    try:
        await _db.collection(_INVITE_CODES_COL).document(code).update(
            {
                "is_used": True,
                "used_by": used_by,
                "used_at": utc_now(),
            }
        )
    except Exception as exc:
        logger.error("mark_code_used(%s) failed: %s", code, exc, exc_info=True)
        raise


class _InviteNotFound(Exception):
    pass


class _InviteAlreadyUsed(Exception):
    pass


class _InviteExpired(Exception):
    pass


@async_transactional
async def _claim_invite_txn(
    transaction: object,
    invite_ref: object,
    user_ref: object,
    new_user_data: dict,
    telegram_id: str,
) -> None:
    """Transactional inner: validate invite, create user, mark code used atomically."""
    from utils.time import as_aware_utc  # local import avoids circular at module level

    invite_snap = await invite_ref.get(transaction=transaction)  # type: ignore[attr-defined]
    user_snap = await user_ref.get(transaction=transaction)  # type: ignore[attr-defined]
    if not invite_snap.exists:
        raise _InviteNotFound()
    invite = invite_snap.to_dict() or {}
    if invite.get("is_used") and invite.get("used_by") != telegram_id:
        raise _InviteAlreadyUsed()
    expires_at = invite.get("expires_at")
    if expires_at and as_aware_utc(expires_at) < utc_now():
        raise _InviteExpired()
    transaction.update(  # type: ignore[union-attr]
        invite_ref,
        {"is_used": True, "used_by": telegram_id, "used_at": utc_now()},
    )
    if not user_snap.exists:
        transaction.set(user_ref, new_user_data)  # type: ignore[union-attr]


async def claim_invite_code_and_create_user(
    code: str,
    telegram_id: str,
    new_user: "User",
) -> None:
    """Atomically validate + claim an invite code and create the user document.

    Raises ValueError with a reason key ("not_found", "already_used", "expired").
    """
    invite_ref = _db.collection(_INVITE_CODES_COL).document(code)
    user_ref = _db.collection(_USERS_COL).document(telegram_id)
    transaction = _db.transaction()
    try:
        await _claim_invite_txn(
            transaction,
            invite_ref,
            user_ref,
            new_user.model_dump(mode="json"),
            telegram_id,
        )
    except _InviteNotFound:
        raise ValueError("not_found")
    except _InviteAlreadyUsed:
        raise ValueError("already_used")
    except _InviteExpired:
        raise ValueError("expired")
    except Exception as exc:
        logger.error(
            "claim_invite_code_and_create_user(%s/%s) failed: %s",
            code, public_user_ref(telegram_id), exc, exc_info=True,
        )
        raise


async def create_invite_code(code: str) -> None:
    """Persist a new invite code document."""
    try:
        invite = InviteCode(code=code, created_at=utc_now())
        await _db.collection(_INVITE_CODES_COL).document(code).set(
            invite.model_dump(mode="json")
        )
    except Exception as exc:
        logger.error("create_invite_code(%s) failed: %s", code, exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Digest scheduling
# ---------------------------------------------------------------------------


async def get_active_users_for_hour(utc_hour: int) -> list[User]:
    """
    Return all active, non-paused users whose delivery_hour_utc matches utc_hour.

    Requires a composite Firestore index on (is_active, is_paused, delivery_hour_utc).
    """
    try:
        query = (
            _db.collection(_USERS_COL)
            .where(filter=FieldFilter("is_active", "==", True))
            .where(filter=FieldFilter("is_paused", "==", False))
            .where(filter=FieldFilter("delivery_hour_utc", "==", utc_hour))
        )
        docs = query.stream()
        users: list[User] = []
        async for doc in docs:
            try:
                users.append(User(**doc.to_dict()))
            except Exception as parse_exc:
                logger.warning(
                    "Skipping malformed user doc %s: %s", doc.id, parse_exc
                )
        return users
    except Exception as exc:
        logger.error("get_active_users_for_hour(%d) failed: %s", utc_hour, exc, exc_info=True)
        raise


async def get_active_users_for_window(utc_hours: list[int]) -> list[User]:
    """
    Return all active, non-paused users whose delivery_hour_utc is in utc_hours.

    Used by /prepare's forward-looking window scan. The IN-query reuses the existing
    composite index on (is_active, is_paused, delivery_hour_utc).
    """
    if not utc_hours:
        return []
    try:
        query = (
            _db.collection(_USERS_COL)
            .where(filter=FieldFilter("is_active", "==", True))
            .where(filter=FieldFilter("is_paused", "==", False))
            .where(filter=FieldFilter("delivery_hour_utc", "in", utc_hours))
        )
        docs = query.stream()
        users: list[User] = []
        async for doc in docs:
            try:
                users.append(User(**doc.to_dict()))
            except Exception as parse_exc:
                logger.warning(
                    "Skipping malformed user doc %s: %s", doc.id, parse_exc
                )
        return users
    except Exception as exc:
        logger.error("get_active_users_for_window(%s) failed: %s", utc_hours, exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Topic weights (learning signal)
# ---------------------------------------------------------------------------


async def update_topic_weights(
    telegram_id: str, topic: str, delta: float
) -> None:
    """
    Apply delta to a topic weight, clamp each weight to [0.5, 3.0],
    then normalise so sum(weights) == len(topics).
    """
    try:
        user = await get_user(telegram_id)
        if user is None:
            logger.warning("update_topic_weights: user %s not found", public_user_ref(telegram_id))
            return

        weights = dict(user.topic_weights)
        if topic in weights:
            weights[topic] = max(0.5, min(3.0, weights[topic] + delta))

        n = len(user.topics)
        if n > 0:
            total = sum(weights.get(t, 1.0) for t in user.topics)
            if total > 0:
                weights = {
                    t: weights.get(t, 1.0) / total * n for t in user.topics
                }
                # Re-clamp after normalisation
                weights = {t: max(0.5, min(3.0, w)) for t, w in weights.items()}

        await update_user(telegram_id, topic_weights=weights)
    except Exception as exc:
        logger.error(
            "update_topic_weights(%s, %s) failed: %s", public_user_ref(telegram_id), topic, exc, exc_info=True
        )
        raise


# ---------------------------------------------------------------------------
# Digest history (per-user-per-day, with reaction tracking)
# ---------------------------------------------------------------------------


def _digest_history_doc_id(telegram_id: str, sent_date: date) -> str:
    """Composite ID = `{public_user_ref}_{YYYY-MM-DD}` for O(1) per-day lookups."""
    return f"{public_user_ref(telegram_id)}_{sent_date.isoformat()}"


async def save_user_digest_history(
    telegram_id: str, articles: list[dict], sent_at: datetime
) -> None:
    """Persist what was delivered to a user on a given UTC day.

    Idempotent: same-day re-runs overwrite the existing doc.
    Failure is logged and swallowed so a history write can't block delivery.
    """
    try:
        sent_date = sent_at.date()
        doc_id = _digest_history_doc_id(telegram_id, sent_date)
        ttl = settings.DIGEST_HISTORY_TTL_SECONDS
        expires_at = sent_at + timedelta(seconds=ttl)
        # Strip any prior reaction state — fresh delivery means fresh reactions
        clean_articles = [
            {
                "article_id": a.get("article_id", ""),
                "url": a.get("url", ""),
                "title": a.get("title", ""),
                "topic": a.get("topic", ""),
                "source": a.get("source", ""),
                "published_at": a.get("published_at", ""),
                "citation_status": a.get("citation_status", "valid"),
                "reaction": None,
                "reacted_at": None,
            }
            for a in articles
        ]
        payload = {
            "user_ref": public_user_ref(telegram_id),
            "date": sent_date.isoformat(),
            "sent_at": sent_at,
            "expires_at": expires_at,
            "articles": clean_articles,
        }
        await _db.collection(_DIGEST_HISTORY_COL).document(doc_id).set(payload)
    except Exception as exc:
        logger.error(
            "save_user_digest_history(%s) failed: %s",
            public_user_ref(telegram_id),
            exc,
            exc_info=True,
        )


async def record_article_reaction(
    telegram_id: str, topic: str, article_id_prefix: str, feedback_type: str
) -> None:
    """Patch the matching article in today's (or yesterday's) history doc.

    Looks for the article by article_id prefix (first 8 chars of UUID). If neither
    today nor yesterday contains a match (TTL expired or stale callback), logs and
    exits silently. Never raises — topic-weights have already been updated by the caller.
    """
    try:
        now = datetime.now(timezone.utc)
        candidates = [now.date(), (now - timedelta(days=1)).date()]
        for day in candidates:
            doc_id = _digest_history_doc_id(telegram_id, day)
            doc_ref = _db.collection(_DIGEST_HISTORY_COL).document(doc_id)
            doc = await doc_ref.get()
            if not doc.exists:
                continue
            data = doc.to_dict() or {}
            articles = list(data.get("articles") or [])
            patched = False
            for a in articles:
                if a.get("article_id", "").startswith(article_id_prefix):
                    a["reaction"] = feedback_type
                    a["reacted_at"] = now
                    patched = True
                    break
            if patched:
                await doc_ref.update({"articles": articles})
                return

        logger.info(
            "record_article_reaction: no matching article for user=%s topic=%r article_id_prefix=%r in last 2 days",
            public_user_ref(telegram_id),
            topic,
            article_id_prefix,
        )
    except Exception as exc:
        logger.error(
            "record_article_reaction(%s) failed: %s",
            public_user_ref(telegram_id),
            exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Pending digests (pre-built by /prepare, consumed by /deliver)
# ---------------------------------------------------------------------------


def _pending_doc_id(telegram_id: str, target_date: date, target_hour: int, target_minute: int = 0) -> str:
    return f"{public_user_ref(telegram_id)}_{target_date.isoformat()}_{target_hour:02d}{target_minute:02d}"


async def save_pending_digest(
    telegram_id: str,
    target_date: date,
    target_hour: int,
    articles: list[dict],
    target_minute: int = 0,
) -> None:
    """Persist a fully-built digest ready for Telegram delivery. Overwrites any existing doc."""
    try:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=settings.PENDING_DIGEST_TTL_SECONDS)
        doc_id = _pending_doc_id(telegram_id, target_date, target_hour, target_minute)
        payload = {
            "telegram_id": telegram_id,
            "target_hour": target_hour,
            "target_minute": target_minute,
            "target_date": target_date.isoformat(),
            "articles": articles,
            "built_at": now,
            "delivered_at": None,
            "expires_at": expires_at,
        }
        await _db.collection(_PENDING_COL).document(doc_id).set(payload)
    except Exception as exc:
        logger.error(
            "save_pending_digest(%s) failed: %s",
            public_user_ref(telegram_id),
            exc,
            exc_info=True,
        )
        raise


async def get_pending_digest(
    telegram_id: str,
    target_date: date,
    target_hour: int,
    target_minute: int = 0,
) -> dict | None:
    """Return a pending digest doc if present and unexpired, else None."""
    try:
        doc_id = _pending_doc_id(telegram_id, target_date, target_hour, target_minute)
        doc = await _db.collection(_PENDING_COL).document(doc_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        expires_at = data.get("expires_at")
        now = datetime.now(timezone.utc)
        if expires_at is not None:
            if isinstance(expires_at, datetime):
                exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            else:
                seconds = getattr(expires_at, "seconds", None)
                exp = datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds else now
            if exp <= now:
                return None
        return data
    except Exception as exc:
        logger.error(
            "get_pending_digest(%s) failed: %s",
            public_user_ref(telegram_id),
            exc,
            exc_info=True,
        )
        return None


async def mark_pending_delivered(
    telegram_id: str,
    target_date: date,
    target_hour: int,
    target_minute: int = 0,
) -> None:
    """Stamp delivered_at on the pending digest doc (does not delete it; TTL handles cleanup)."""
    try:
        doc_id = _pending_doc_id(telegram_id, target_date, target_hour, target_minute)
        await _db.collection(_PENDING_COL).document(doc_id).update(
            {"delivered_at": datetime.now(timezone.utc)}
        )
    except Exception as exc:
        logger.error(
            "mark_pending_delivered(%s) failed: %s",
            public_user_ref(telegram_id),
            exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------


async def get_all_users() -> list[User]:
    """Return every user document — used by admin /users command."""
    try:
        docs = _db.collection(_USERS_COL).stream()
        users: list[User] = []
        async for doc in docs:
            try:
                users.append(User(**doc.to_dict()))
            except Exception as parse_exc:
                logger.warning("Skipping malformed user doc %s: %s", doc.id, parse_exc)
        return users
    except Exception as exc:
        logger.error("get_all_users failed: %s", exc, exc_info=True)
        raise


async def get_all_active_users() -> list[User]:
    """Return all is_active users — used by admin /broadcast command."""
    try:
        query = _db.collection(_USERS_COL).where(filter=FieldFilter("is_active", "==", True))
        docs = query.stream()
        users: list[User] = []
        async for doc in docs:
            try:
                users.append(User(**doc.to_dict()))
            except Exception as parse_exc:
                logger.warning("Skipping malformed user doc %s: %s", doc.id, parse_exc)
        return users
    except Exception as exc:
        logger.error("get_all_active_users failed: %s", exc, exc_info=True)
        raise
