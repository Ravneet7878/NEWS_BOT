"""PTB CallbackQueryHandler for 👍/👎 inline feedback buttons."""

from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

import shared.database as db
from shared.models import Feedback
from utils.logging import get_logger

logger = get_logger(__name__)


async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Process inline keyboard feedback callbacks.
    Callback data format: "feedback:{more|less}:{topic}:{title[:40]}"
    Topic and title may contain colons — use maxsplit=3.
    """
    query = update.callback_query
    assert query is not None
    assert query.from_user is not None

    telegram_id = str(query.from_user.id)

    try:
        data = query.data or ""
        parts = data.split(":", maxsplit=3)
        if len(parts) != 4 or parts[0] != "feedback":
            await query.answer("Unknown action.")
            return

        _, feedback_type, topic, article_title = parts

        if feedback_type not in ("more", "less"):
            await query.answer("Unknown feedback type.")
            return

        delta = 0.1 if feedback_type == "more" else -0.1

        await db.update_topic_weights(telegram_id, topic, delta)
        await db.save_feedback(
            Feedback(
                user_id=telegram_id,
                topic=topic,
                article_title=article_title,
                feedback_type=feedback_type,
                created_at=datetime.utcnow(),
                digest_date=date.today(),
            )
        )

        await query.answer("Got it! Adjusting your feed.")
    except Exception as exc:
        logger.error("handle_feedback(%s) failed: %s", telegram_id, exc, exc_info=True)
        try:
            await query.answer("Something went wrong. Please try again.")
        except Exception:
            pass
