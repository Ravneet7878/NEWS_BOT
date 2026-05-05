"""ADK-compatible Telegram delivery tools for the worker pipeline."""

import html
import json
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, RetryAfter, TimedOut
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from shared.config import settings
from utils.logging import get_logger
from utils.privacy import public_user_ref

logger = get_logger(__name__)

# Module-level singleton — one HTTPS session for the lifetime of the worker process
_bot: Bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

_MAX_MESSAGE_LENGTH = 4096
_CALLBACK_MAX = 64  # Telegram hard limit on callback_data bytes


@retry(
    stop=stop_after_attempt(settings.RETRY_MAX_ATTEMPTS),
    wait=wait_random_exponential(
        multiplier=settings.RETRY_BACKOFF_BASE_SECONDS, max=10.0
    ),
    retry=retry_if_exception_type((TimedOut, NetworkError, RetryAfter)),
    reraise=True,
)
async def _send_with_retry(**kwargs: object) -> object:
    return await _bot.send_message(**kwargs)


def _escape_with_bold(text: str) -> str:
    """Escape HTML but preserve <b>...</b> tags produced by the summariser."""
    parts = re.split(r"(<b>|</b>)", text)
    return "".join(p if p in ("<b>", "</b>") else html.escape(p) for p in parts)


def _byte_truncate(s: str, max_bytes: int) -> str:
    """Truncate s so its UTF-8 encoding fits within max_bytes."""
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    # Drop any incomplete multi-byte sequence at the cut point
    return enc[:max_bytes].decode("utf-8", errors="ignore")


def _build_feedback_keyboard(topic: str, article_id: str) -> InlineKeyboardMarkup:
    """Build the 👍/👎 inline keyboard for a single article."""
    # Budget: 64 - "feedback:more:" (14) - ":" sep (1) - short_id (8) = 41 bytes for topic.
    safe_topic = _byte_truncate(topic.replace(":", "-"), 41)
    short_id = article_id[:8]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👍 More like this",
                    callback_data=f"feedback:more:{safe_topic}:{short_id}",
                ),
                InlineKeyboardButton(
                    "👎 Less like this",
                    callback_data=f"feedback:less:{safe_topic}:{short_id}",
                ),
            ]
        ]
    )


def _build_article_body(
    title: str,
    meta_line: str,
    points: list[str],
    why: str,
) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"🔥 <b>{html.escape(title)}</b>")
    if meta_line:
        parts.append(meta_line)
    if points:
        parts.append("")
        for pt in points:
            parts.append(f"• {_escape_with_bold(pt)}")
    if why:
        parts.append("")
        parts.append(f"💡 <b>Why it matters:</b> {_escape_with_bold(why)}")
    return "\n".join(parts).strip()


async def send_digest_message(telegram_id: str, digest_text: str) -> dict:
    """
    Send a formatted news digest to a Telegram user.

    Each article is sent as a separate HTML message with topic, sourced link,
    6–7 summary bullet points, why it matters, and 👍/👎 InlineKeyboard.
    Messages exceeding Telegram's 4096-char limit are trimmed safely.

    Returns: {"status": "delivered", "messages_sent": int}
    Raises on Telegram network/API failure (after retries exhaust).
    """
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
        citation_status: str = article.get("citation_status", "valid")
        published_at: str = article.get("published_at", "")
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
        safe_url = html.escape(url, quote=True) if url_valid else ""

        date_str = f"Date: {published_at}" if published_at else "Date: Unknown"

        # Combined topic + source + date meta line
        if source and url_valid and citation_status == "valid":
            meta_line = (
                f'🏷 <b>{html.escape(topic)}</b> • 📰 <a href="{safe_url}">{html.escape(source)}</a>'
                f" | {html.escape(date_str)}"
            )
        elif source and citation_status == "blocked":
            meta_line = (
                f"🏷 <b>{html.escape(topic)}</b> • 📰 {html.escape(source)} "
                f"(citation unavailable: publisher blocked verification) | {html.escape(date_str)}"
            )
        elif source and not url_valid:
            meta_line = (
                f"🏷 <b>{html.escape(topic)}</b> • 📰 {html.escape(source)} "
                f"(citation unavailable: no URL) | {html.escape(date_str)}"
            )
        elif source:
            meta_line = (
                f"🏷 <b>{html.escape(topic)}</b> • 📰 {html.escape(source)}"
                f" | {html.escape(date_str)}"
            )
        elif topic:
            meta_line = f"🏷 <b>{html.escape(topic)}</b> | {html.escape(date_str)}"
        else:
            meta_line = html.escape(date_str) if published_at else ""

        body = _build_article_body(title, meta_line, points, why)

        # Trim strategy 1: remove trailing summary_points one at a time
        while len(body) > _MAX_MESSAGE_LENGTH and len(points) > 1:
            points.pop()
            body = _build_article_body(title, meta_line, points, why)

        # Trim strategy 2: shorten why_it_matters
        if len(body) > _MAX_MESSAGE_LENGTH:
            while len(body) > _MAX_MESSAGE_LENGTH and len(why) > 10:
                why = why[:-10]
                body = _build_article_body(title, meta_line, points, why + "…")

        # Hard fallback
        if len(body) > _MAX_MESSAGE_LENGTH:
            body = body[: _MAX_MESSAGE_LENGTH - 1] + "…"

        article_id: str = article.get("article_id", "")
        keyboard = _build_feedback_keyboard(topic, article_id) if article_id and topic else None

        await _send_with_retry(
            chat_id=telegram_id,
            text=body,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        messages_sent += 1

    return {"status": "delivered", "messages_sent": messages_sent}


async def send_error_to_user(telegram_id: str, error_message: str) -> dict:
    """
    Send a plain-text error notification to a Telegram user.

    Returns: {"status": "sent"}
    Raises on Telegram network/API failure (after retries exhaust).
    """
    await _send_with_retry(chat_id=telegram_id, text=error_message)
    return {"status": "sent"}
