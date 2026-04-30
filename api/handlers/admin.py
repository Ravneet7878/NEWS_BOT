"""PTB command handlers for admin-only operations."""

import asyncio
import secrets

from telegram import Update
from telegram.ext import ContextTypes

import shared.database as db
from shared.config import settings
from shared.models import OnboardingState
from utils.logging import get_logger

logger = get_logger(__name__)


def _is_admin(update: Update) -> bool:
    """Return True only if the sender is the configured admin."""
    assert update.effective_user is not None
    return str(update.effective_user.id) == settings.ADMIN_TELEGRAM_ID


async def handle_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /gencode <N> — generate N invite codes.
    Silently ignores requests from non-admin users.
    """
    assert update.message is not None

    if not _is_admin(update):
        return

    try:
        args = context.args or []
        try:
            count = int(args[0]) if args else 1
            if count < 1 or count > 50:
                raise ValueError("out of range")
        except ValueError:
            await update.message.reply_text("Usage: /gencode <N>  (1–50)")
            return

        codes: list[str] = []
        for _ in range(count):
            code = secrets.token_urlsafe(6).upper()
            await db.create_invite_code(code)
            codes.append(code)

        codes_display = "\n".join(f"  `{c}`" for c in codes)
        await update.message.reply_text(
            f"Generated {count} invite code(s):\n{codes_display}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("handle_gencode failed: %s", exc, exc_info=True)
        await update.message.reply_text("Something went wrong generating codes.")


async def handle_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /users — summary table of all users.
    Silently ignores requests from non-admin users.
    """
    assert update.message is not None

    if not _is_admin(update):
        return

    try:
        users = await db.get_all_users()
        total = len(users)
        active = sum(1 for u in users if u.is_active and not u.is_paused)
        paused = sum(1 for u in users if u.is_paused)
        incomplete = sum(
            1 for u in users if u.onboarding_state != OnboardingState.DONE
        )

        await update.message.reply_text(
            f"👥 User Summary\n\n"
            f"Total:               {total}\n"
            f"Active:              {active}\n"
            f"Paused:              {paused}\n"
            f"Onboarding pending:  {incomplete}"
        )
    except Exception as exc:
        logger.error("handle_users failed: %s", exc, exc_info=True)
        await update.message.reply_text("Something went wrong fetching users.")


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /broadcast <message> — send a message to all active users.
    Silently ignores requests from non-admin users.
    """
    assert update.message is not None

    if not _is_admin(update):
        return

    try:
        message_text = " ".join(context.args or []).strip()
        if not message_text:
            await update.message.reply_text("Usage: /broadcast <message>")
            return

        users = await db.get_all_active_users()
        if not users:
            await update.message.reply_text("No active users to broadcast to.")
            return

        sent = 0
        failed = 0
        bot = update.get_bot()

        for user in users:
            try:
                await bot.send_message(chat_id=user.telegram_id, text=message_text)
                sent += 1
            except Exception as send_exc:
                logger.warning(
                    "Broadcast failed for user %s: %s", user.telegram_id, send_exc
                )
                failed += 1
            # Respect Telegram rate limits
            await asyncio.sleep(0.05)

        await update.message.reply_text(
            f"Broadcast complete: {sent} sent, {failed} failed."
        )
    except Exception as exc:
        logger.error("handle_broadcast failed: %s", exc, exc_info=True)
        await update.message.reply_text("Something went wrong during broadcast.")
