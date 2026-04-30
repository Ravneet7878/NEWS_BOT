"""ADK-compatible Firestore tools for the worker pipeline."""

import shared.database as db
from utils.logging import get_logger

logger = get_logger(__name__)


async def get_user_preferences(telegram_id: str) -> dict:
    """
    Retrieve topic list and weights for a user from Firestore.

    Returns: {"topics": list[str], "topic_weights": dict[str, float]}
    """
    try:
        user = await db.get_user(telegram_id)
        if user is None:
            logger.warning("get_user_preferences: user %s not found", telegram_id)
            return {"error": f"User {telegram_id} not found"}
        return {"topics": user.topics, "topic_weights": user.topic_weights}
    except Exception as exc:
        logger.error(
            "get_user_preferences(%s) failed: %s", telegram_id, exc, exc_info=True
        )
        return {"error": str(exc)}


async def save_updated_weights(telegram_id: str, updated_weights: dict) -> dict:
    """
    Persist updated topic weights to Firestore after digest delivery.

    Returns: {"status": "saved"}
    """
    try:
        await db.update_user(telegram_id, topic_weights=updated_weights)
        return {"status": "saved"}
    except Exception as exc:
        logger.error(
            "save_updated_weights(%s) failed: %s", telegram_id, exc, exc_info=True
        )
        return {"error": str(exc)}
