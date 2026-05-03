"""PTB ConversationHandler for invite-code-gated user onboarding."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import shared.database as db
from shared.config import settings
from shared.models import OnboardingState, User
from utils.logging import get_logger

logger = get_logger(__name__)

# Exported state constants consumed by api/main.py
AWAITING_TOPICS: int = 1
AWAITING_TIME: int = 2

_TZ_ABBREVIATIONS: dict[str, str] = {
    "IST": "Asia/Kolkata",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "GMT": "UTC",
    "UTC": "UTC",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "BST": "Europe/London",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
}

_TIME_PATTERN = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*([A-Z]{2,5})?$",
    re.IGNORECASE,
)


def _parse_hour(text: str) -> tuple[int, str] | None:
    """
    Parse a time string and return (local_hour_24, tz_name) or None on failure.
    Accepts: "7", "7am", "7:00", "7:00 AM", "19:00", "7am IST"
    """
    match = _TIME_PATTERN.match(text.strip())
    if not match:
        return None

    hour = int(match.group(1))
    ampm = (match.group(3) or "").lower()
    tz_abbr = (match.group(4) or "").upper()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if hour < 0 or hour > 23:
        return None

    tz_name = _TZ_ABBREVIATIONS.get(tz_abbr, "Asia/Kolkata")
    return hour, tz_name


def _local_to_utc_hour(local_hour: int, tz_name: str) -> int:
    """Convert a local hour to UTC using zoneinfo (Python 3.12 built-in)."""
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Kolkata")

    now = datetime.now(tz=tz)
    local_dt = now.replace(hour=local_hour, minute=0, second=0, microsecond=0)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.hour


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /start <invite_code>.
    Validates invite code, creates user, transitions to AWAITING_TOPICS.
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)

    try:
        existing_user = await db.get_user(telegram_id)
        if existing_user is not None and existing_user.onboarding_state == OnboardingState.DONE:
            await update.message.reply_text(
                f"Welcome back, {existing_user.first_name}! "
                f"You're already registered. Use /status to see your settings or /help for commands."
            )
            return ConversationHandler.END

        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Please use your invite link or send /start <invite_code> to join."
            )
            return ConversationHandler.END

        code = args[0].strip().upper()
        invite = await db.get_invite_code(code)

        if invite is None:
            await update.message.reply_text("That invite code doesn't exist. Please check and try again.")
            return ConversationHandler.END

        if invite.is_used and invite.used_by != telegram_id:
            await update.message.reply_text("That invite code has already been used.")
            return ConversationHandler.END

        if invite.expires_at and invite.expires_at < datetime.utcnow():
            await update.message.reply_text("That invite code has expired.")
            return ConversationHandler.END

        if existing_user is None:
            new_user = User(
                telegram_id=telegram_id,
                first_name=update.effective_user.first_name or "User",
                username=update.effective_user.username,
                invite_code_used=code,
                created_at=datetime.utcnow(),
                onboarding_state=OnboardingState.AWAITING_TOPICS,
            )
            await db.create_user(new_user)
            await db.mark_code_used(code, telegram_id)

        first_name = update.effective_user.first_name or "there"
        await update.message.reply_text(
            f"Hi {first_name}! 🎉 Your invite code is valid.\n\n"
            "What topics would you like to follow?\n"
            "Send a comma-separated list, e.g.:\n"
            "  Technology, Finance, Climate, India\n\n"
            f"You can list 1–{settings.MAX_TOPICS} topics."
        )
        return AWAITING_TOPICS

    except Exception as exc:
        logger.error("handle_start failed for user %s: %s", telegram_id, exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")
        return ConversationHandler.END


async def handle_topics_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Parse comma-separated topics, validate count, initialise weights, prompt for time.
    """
    assert update.effective_user is not None
    assert update.message is not None
    assert update.message.text is not None

    telegram_id = str(update.effective_user.id)

    try:
        raw = update.message.text.strip()
        topics = list(dict.fromkeys(t.strip() for t in raw.split(",") if t.strip()))

        if not 1 <= len(topics) <= settings.MAX_TOPICS:
            await update.message.reply_text(
                f"Please send between 1 and {settings.MAX_TOPICS} topics, separated by commas."
            )
            return AWAITING_TOPICS

        weights = {topic: 1.0 for topic in topics}
        await db.update_user(
            telegram_id,
            topics=topics,
            topic_weights=weights,
            onboarding_state=OnboardingState.AWAITING_TIME,
        )

        topics_display = "\n".join(f"  • {t}" for t in topics)
        await update.message.reply_text(
            f"Got it! I'll cover:\n{topics_display}\n\n"
            "What time should I deliver your daily digest?\n"
            "Examples: 7am, 7am IST, 19:00, 8:30 PM EST"
        )
        return AWAITING_TIME

    except Exception as exc:
        logger.error("handle_topics_input failed for user %s: %s", telegram_id, exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")
        return AWAITING_TOPICS


async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Parse delivery time, convert to UTC, mark user active, end conversation.
    """
    assert update.effective_user is not None
    assert update.message is not None
    assert update.message.text is not None

    telegram_id = str(update.effective_user.id)

    try:
        raw = update.message.text.strip()
        parsed = _parse_hour(raw)

        if parsed is None:
            await update.message.reply_text(
                "I couldn't understand that time. Try formats like:\n"
                "  7am  |  7:00  |  19:00  |  7am IST  |  9pm EST"
            )
            return AWAITING_TIME

        local_hour, tz_name = parsed
        utc_hour = _local_to_utc_hour(local_hour, tz_name)

        await db.update_user(
            telegram_id,
            delivery_hour_utc=utc_hour,
            delivery_tz=tz_name,
            is_active=True,
            onboarding_state=OnboardingState.DONE,
        )

        await update.message.reply_text(
            f"All set! ✅\n\n"
            f"Delivery time: {local_hour:02d}:00 {tz_name} (UTC {utc_hour:02d}:00)\n\n"
            "Your first digest will arrive at the scheduled time.\n"
            "Use /help to see all available commands."
        )
        return ConversationHandler.END

    except Exception as exc:
        logger.error("handle_time_input failed for user %s: %s", telegram_id, exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")
        return AWAITING_TIME
