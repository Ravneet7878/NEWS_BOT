"""ADK-compatible Telegram delivery tools for the worker pipeline."""

import json
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from shared.config import settings

logger = logging.getLogger(__name__)

# Module-level singleton — one HTTPS session for the lifetime of the worker process
_bot: Bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

_MAX_MESSAGE_LENGTH = 4096


def _build_feedback_keyboard(topic: str, title: str) -> InlineKeyboardMarkup:
    """Build the 👍/👎 inline keyboard for a single article."""
    safe_title = title[:40].replace(":", "-")
    safe_topic = topic.replace(":", "-")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👍 More like this",
                    callback_data=f"feedback:more:{safe_topic}:{safe_title}",
                ),
                InlineKeyboardButton(
                    "👎 Less like this",
                    callback_data=f"feedback:less:{safe_topic}:{safe_title}",
                ),
            ]
        ]
    )


async def send_digest_message(telegram_id: str, digest_text: str) -> dict:
    """
    Send a formatted news digest to a Telegram user.

    Splits digest_text on "---" separator into individual article blocks.
    Each block is sent as a separate message with a 👍/👎 InlineKeyboard.
    Truncates any block that would exceed Telegram's 4096-char limit.

    Returns: {"status": "delivered", "messages_sent": int}
    """
    try:
        # digest_text may be a JSON array (from ADK session state) or plain text
        articles: list[dict] = []
        try:
            articles = json.loads(digest_text)
        except (json.JSONDecodeError, TypeError):
            # Fall back to "---" split plain text
            blocks = [b.strip() for b in digest_text.split("---") if b.strip()]
            articles = [{"title": "", "url": "", "topic": "", "summary": b} for b in blocks]

        messages_sent = 0
        for article in articles:
            title: str = article.get("title", "")
            url: str = article.get("url", "")
            topic: str = article.get("topic", "")
            summary: str = article.get("summary", "")

            header = f"*{title}*\n" if title else ""
            source_line = f"\n[Read more]({url})\n" if url else ""
            body = f"{header}{summary}{source_line}".strip()

            # Truncate to stay within Telegram's limit
            if len(body) > _MAX_MESSAGE_LENGTH:
                body = body[: _MAX_MESSAGE_LENGTH - 3] + "..."

            keyboard = _build_feedback_keyboard(topic, title) if title and topic else None

            await _bot.send_message(
                chat_id=telegram_id,
                text=body,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            messages_sent += 1

        return {"status": "delivered", "messages_sent": messages_sent}
    except Exception as exc:
        logger.error(
            "send_digest_message(%s) failed: %s", telegram_id, exc, exc_info=True
        )
        return {"error": str(exc)}


async def send_error_to_user(telegram_id: str, error_message: str) -> dict:
    """
    Send a plain-text error notification to a Telegram user.

    Returns: {"status": "sent"}
    """
    try:
        await _bot.send_message(chat_id=telegram_id, text=error_message)
        return {"status": "sent"}
    except Exception as exc:
        logger.error(
            "send_error_to_user(%s) failed: %s", telegram_id, exc, exc_info=True
        )
        return {"error": str(exc)}
