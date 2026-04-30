"""ADK-compatible Telegram delivery tools for the worker pipeline."""

import html
import json
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from shared.config import settings
from utils.logging import get_logger

logger = get_logger(__name__)

# Module-level singleton — one HTTPS session for the lifetime of the worker process
_bot: Bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

_MAX_MESSAGE_LENGTH = 4096
_CALLBACK_MAX = 64  # Telegram hard limit on callback_data bytes
_SECTION_HEADINGS = ["Key Points", "What's happening", "The gist", "Quick breakdown"]


def _escape_with_bold(text: str) -> str:
    """Escape HTML but preserve <b>...</b> tags produced by the summariser."""
    parts = re.split(r"(<b>|</b>)", text)
    return "".join(p if p in ("<b>", "</b>") else html.escape(p) for p in parts)


def _build_feedback_keyboard(topic: str, title: str) -> InlineKeyboardMarkup:
    """Build the 👍/👎 inline keyboard for a single article."""
    prefix = "feedback:more:"  # "more" and "less" are the same length (14 chars)
    budget = _CALLBACK_MAX - len(prefix) - 1  # -1 for ":" between topic and title → 49
    safe_topic = topic.replace(":", "-")[:20]
    safe_title = title.replace(":", "-")[: budget - len(safe_topic)]
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


def _build_article_body(
    title: str,
    meta_line: str,
    points: list[str],
    why: str,
    heading_index: int = 0,
) -> str:
    heading = _SECTION_HEADINGS[heading_index % len(_SECTION_HEADINGS)]
    parts: list[str] = []
    if title:
        parts.append(f"🔥 <b>{html.escape(title)}</b>")
    parts.append("")
    if meta_line:
        parts.append(meta_line)
    if points:
        parts.append("")
        parts.append(f"<b>{heading}:</b>")
        for pt in points:
            parts.append(f"• {_escape_with_bold(pt)}")
    if why:
        parts.append("")
        parts.append("💡 <b>Why it matters:</b>")
        parts.append(_escape_with_bold(why))
    return "\n".join(parts).strip()


async def send_digest_message(telegram_id: str, digest_text: str) -> dict:
    """
    Send a formatted news digest to a Telegram user.

    Each article is sent as a separate HTML message with topic, sourced link,
    6–7 summary bullet points, why it matters, and 👍/👎 InlineKeyboard.
    Messages exceeding Telegram's 4096-char limit are trimmed safely.

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
            topic: str = article.get("topic", "")
            source: str = article.get("source", "")
            url: str = article.get("url", "")
            points: list[str] = list(article.get("summary_points") or [])
            why: str = article.get("why_it_matters", "")

            # Backward compatibility: old-format articles have a flat "summary" field
            if not points and not why:
                legacy = article.get("summary", "")
                if legacy:
                    points = [legacy]

            # Deduplicate bullets (preserve order, case-insensitive)
            seen: set[str] = set()
            unique_points: list[str] = []
            for p in points:
                if p.lower() not in seen:
                    seen.add(p.lower())
                    unique_points.append(p)
            points = unique_points

            if len(points) < 5:
                logger.warning("Article '%s' has only %d bullets (< 5)", title, len(points))

            # URL validation — only link if it starts with http:// or https://
            url_valid = isinstance(url, str) and url.startswith(("http://", "https://"))

            # Combined topic + source meta line
            if source and url_valid:
                meta_line = f'🏷 <b>{html.escape(topic)}</b> • 📰 <a href="{url}">{html.escape(source)}</a>'
            elif source:
                meta_line = f'🏷 <b>{html.escape(topic)}</b> • 📰 {html.escape(source)}'
            elif topic:
                meta_line = f'🏷 <b>{html.escape(topic)}</b>'
            else:
                meta_line = ""

            body = _build_article_body(title, meta_line, points, why, heading_index=messages_sent)

            # Trim strategy 1: remove trailing summary_points one at a time
            while len(body) > _MAX_MESSAGE_LENGTH and len(points) > 1:
                points.pop()
                body = _build_article_body(title, meta_line, points, why, heading_index=messages_sent)

            # Trim strategy 2: shorten why_it_matters
            if len(body) > _MAX_MESSAGE_LENGTH:
                while len(body) > _MAX_MESSAGE_LENGTH and len(why) > 10:
                    why = why[:-10]
                    body = _build_article_body(title, meta_line, points, why + "…", heading_index=messages_sent)

            # Hard fallback
            if len(body) > _MAX_MESSAGE_LENGTH:
                body = body[: _MAX_MESSAGE_LENGTH - 1] + "…"

            keyboard = _build_feedback_keyboard(topic, title) if title and topic else None

            await _bot.send_message(
                chat_id=telegram_id,
                text=body,
                parse_mode="HTML",
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
