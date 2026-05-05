"""PTB command handlers for registered users."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.ext import ContextTypes

import shared.database as db
from api.handlers.onboarding import _parse_time, _local_to_utc_hm
from shared.config import settings
from shared.models import OnboardingState
from utils.guardrails import sanitize_topics
from utils.logging import get_logger
from utils.privacy import public_user_ref
from utils.time import utc_now

logger = get_logger(__name__)


async def _require_user(update: Update) -> bool:
    """Reply with an error and return False if the user is not registered."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    user = await db.get_user(telegram_id)
    if user is None:
        await update.message.reply_text(
            "You are not registered. Use your invite link to sign up first."
        )
        return False
    return True


async def handle_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause digest delivery."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return
        await db.update_user(telegram_id, is_paused=True)
        await update.message.reply_text(
            "Digest delivery paused. Send /resume whenever you want to start again."
        )
    except Exception as exc:
        logger.error("handle_pause(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume digest delivery."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return
        await db.update_user(telegram_id, is_paused=False)
        await update.message.reply_text("Digest delivery resumed. ✅")
    except Exception as exc:
        logger.error("handle_resume(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Replace the user's topic list.
    Usage: /topics Technology, Finance, Climate
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return

        user = await db.get_user(telegram_id)
        assert user is not None

        raw = " ".join(context.args or [])
        if not raw.strip():
            await update.message.reply_text(
                f"Usage: /topics Technology, Finance, Climate\n"
                f"Send 1–{settings.MAX_TOPICS} comma-separated topics."
            )
            return

        raw_split = [t.strip() for t in raw.split(",")]
        try:
            new_topics = sanitize_topics(raw_split)
        except ValueError as policy_err:
            await update.message.reply_text(str(policy_err))
            return

        # Preserve existing weights where the topic name matches
        old_weights = user.topic_weights
        new_weights = {t: old_weights.get(t, 1.0) for t in new_topics}

        await db.update_user(telegram_id, topics=new_topics, topic_weights=new_weights)
        topics_display = ", ".join(new_topics)
        await update.message.reply_text(f"Topics updated: {topics_display}")
    except Exception as exc:
        logger.error("handle_topics(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Change the delivery time.
    Usage: /time 7am IST
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return

        raw = " ".join(context.args or [])
        if not raw.strip():
            await update.message.reply_text(
                "Usage: /time 7am IST\n"
                "Accepted formats: 7, 7am, 7:00, 7:00 AM, 07:00, 19:00 (optionally followed by timezone abbreviation)"
            )
            return

        parsed = _parse_time(raw)
        if parsed is None:
            await update.message.reply_text(
                "I couldn't understand that time. Try: 7am  |  19:00  |  7am IST  |  9pm EST"
            )
            return

        local_hour, local_minute, tz_name = parsed
        utc_hour, utc_minute = _local_to_utc_hm(local_hour, local_minute, tz_name)

        user = await db.get_user(telegram_id)
        assert user is not None

        extra: dict = {}
        if not user.is_active and user.topics:
            extra = {"is_active": True, "onboarding_state": OnboardingState.DONE}

        await db.update_user(
            telegram_id,
            delivery_hour_utc=utc_hour,
            delivery_minute_utc=utc_minute,
            delivery_tz=tz_name,
            **extra,
        )
        await update.message.reply_text(
            f"Delivery time updated to {local_hour:02d}:{local_minute:02d} {tz_name} (UTC {utc_hour:02d}:{utc_minute:02d}). ✅"
        )
    except Exception as exc:
        logger.error("handle_time(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current user settings."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        user = await db.get_user(telegram_id)
        if user is None:
            await update.message.reply_text(
                "You are not registered. Use your invite link to sign up first."
            )
            return

        status_str = "active" if user.is_active and not user.is_paused else ("paused" if user.is_paused else "inactive")

        # Next delivery time in user's local tz
        try:
            tz = ZoneInfo(user.delivery_tz)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")

        # Convert stored UTC delivery hour+minute back to local for display
        utc_delivery = utc_now().replace(
            hour=user.delivery_hour_utc, minute=user.delivery_minute_utc, second=0, microsecond=0, tzinfo=ZoneInfo("UTC")
        )
        local_delivery = utc_delivery.astimezone(tz)
        delivery_display = local_delivery.strftime("%I:%M %p") + f" {user.delivery_tz}"

        topics_display = "\n".join(
            f"  • {t} (weight: {user.topic_weights.get(t, 1.0):.2f})"
            for t in user.topics
        ) or "  None set"

        last_sent = (
            user.last_digest_sent.strftime("%Y-%m-%d %H:%M UTC")
            if user.last_digest_sent
            else "Never"
        )

        await update.message.reply_text(
            f"📊 Your Status\n\n"
            f"Status: {status_str}\n"
            f"Delivery: {delivery_display}\n"
            f"Total digests sent: {user.total_digests_sent}\n"
            f"Last digest: {last_sent}\n\n"
            f"Topics:\n{topics_display}"
        )
    except Exception as exc:
        logger.error("handle_status(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for deletion confirmation."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return
        await update.message.reply_text(
            "⚠️ This will permanently delete your account and all your data.\n\n"
            "Send /confirmdelete to proceed, or ignore this message to cancel."
        )
    except Exception as exc:
        logger.error("handle_delete(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_confirmdelete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Permanently delete the user's Firestore document."""
    assert update.effective_user is not None
    assert update.message is not None

    telegram_id = str(update.effective_user.id)
    try:
        if not await _require_user(update):
            return
        await db.delete_user_data(telegram_id)
        await update.message.reply_text(
            "Your account has been deleted. Goodbye! 👋\n"
            "If you ever want to return, you'll need a new invite code."
        )
    except Exception as exc:
        logger.error("handle_confirmdelete(%s) failed: %s", public_user_ref(telegram_id), exc, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the command reference."""
    assert update.message is not None

    try:
        await update.message.reply_text(
            "📰 News Bot Commands\n\n"
            "/status         — View your current settings\n"
            "/topics <list>  — Update topics (comma-separated)\n"
            "/time <time>    — Change delivery time (e.g. 7am IST)\n"
            "/pause          — Pause digest delivery\n"
            "/resume         — Resume digest delivery\n"
            "/delete         — Delete your account (requires confirmation)\n"
            "/help           — Show this message"
        )
    except Exception as exc:
        logger.error("handle_help failed: %s", exc, exc_info=True)
