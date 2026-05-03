"""Async Firestore helper functions — module-level singleton, no wrapper classes."""

from datetime import datetime

from google.cloud.firestore_v1 import FieldFilter  # type: ignore[import-untyped]
from google.cloud.firestore_v1.async_client import AsyncClient  # type: ignore[import-untyped]

from shared.config import settings
from shared.models import Feedback, InviteCode, OnboardingState, User
from utils.logging import get_logger

logger = get_logger(__name__)

# Module-level singleton — one connection pool for the lifetime of the process
_db: AsyncClient = AsyncClient(project=settings.GCP_PROJECT_ID)

_USERS_COL = "users"
_INVITE_CODES_COL = "invite_codes"
_FEEDBACK_COL = "feedback"


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
        logger.error("get_user(%s) failed: %s", telegram_id, exc, exc_info=True)
        raise


async def create_user(user: User) -> None:
    """Persist a new User document to Firestore."""
    try:
        await _db.collection(_USERS_COL).document(user.telegram_id).set(
            user.model_dump(mode="json")
        )
    except Exception as exc:
        logger.error("create_user(%s) failed: %s", user.telegram_id, exc, exc_info=True)
        raise


async def update_user(telegram_id: str, **fields: object) -> None:
    """Partial-update specific fields on an existing User document."""
    try:
        await _db.collection(_USERS_COL).document(telegram_id).update(fields)
    except Exception as exc:
        logger.error("update_user(%s) failed: %s", telegram_id, exc, exc_info=True)
        raise


async def delete_user(telegram_id: str) -> None:
    """Delete a User document from Firestore."""
    try:
        await _db.collection(_USERS_COL).document(telegram_id).delete()
    except Exception as exc:
        logger.error("delete_user(%s) failed: %s", telegram_id, exc, exc_info=True)
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
                "used_at": datetime.utcnow(),
            }
        )
    except Exception as exc:
        logger.error("mark_code_used(%s) failed: %s", code, exc, exc_info=True)
        raise


async def create_invite_code(code: str) -> None:
    """Persist a new invite code document."""
    try:
        invite = InviteCode(code=code, created_at=datetime.utcnow())
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


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


async def save_feedback(feedback: Feedback) -> None:
    """Append a feedback record to the feedback collection."""
    try:
        await _db.collection(_FEEDBACK_COL).add(feedback.model_dump(mode="json"))
    except Exception as exc:
        logger.error("save_feedback failed: %s", exc, exc_info=True)
        raise


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
            logger.warning("update_topic_weights: user %s not found", telegram_id)
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
            "update_topic_weights(%s, %s) failed: %s", telegram_id, topic, exc, exc_info=True
        )
        raise


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
