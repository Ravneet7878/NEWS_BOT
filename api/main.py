"""API FastAPI service — hosts the Telegram webhook."""

import secrets
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from api.handlers import admin, commands, feedback, onboarding
from shared.config import settings
from utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Build and initialise the PTB Application, tear it down on shutdown."""
    ptb_app: Application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Onboarding conversation — must be registered before plain CommandHandlers
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", onboarding.handle_start)],
        states={
            onboarding.AWAITING_TOPICS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    onboarding.handle_topics_input,
                )
            ],
            onboarding.AWAITING_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    onboarding.handle_time_input,
                )
            ],
        },
        fallbacks=[CommandHandler("help", commands.handle_help)],
        persistent=False,
    )
    ptb_app.add_handler(conv_handler)

    # User commands
    ptb_app.add_handler(CommandHandler("pause", commands.handle_pause))
    ptb_app.add_handler(CommandHandler("resume", commands.handle_resume))
    ptb_app.add_handler(CommandHandler("topics", commands.handle_topics))
    ptb_app.add_handler(CommandHandler("time", commands.handle_time))
    ptb_app.add_handler(CommandHandler("status", commands.handle_status))
    ptb_app.add_handler(CommandHandler("delete", commands.handle_delete))
    ptb_app.add_handler(CommandHandler("confirmdelete", commands.handle_confirmdelete))
    ptb_app.add_handler(CommandHandler("help", commands.handle_help))

    # Admin commands
    ptb_app.add_handler(CommandHandler("gencode", admin.handle_gencode))
    ptb_app.add_handler(CommandHandler("users", admin.handle_users))
    ptb_app.add_handler(CommandHandler("broadcast", admin.handle_broadcast))

    # Inline feedback buttons
    ptb_app.add_handler(
        CallbackQueryHandler(feedback.handle_feedback, pattern=r"^feedback:")
    )

    await ptb_app.initialize()
    app.state.ptb_app = ptb_app

    if settings.APP_ENV not in ("local", "test") and not settings.LOG_PSEUDONYM_SALT:
        raise RuntimeError("LOG_PSEUDONYM_SALT must be set in non-local environments")

    logger.info("PTB Application initialised")

    yield

    await ptb_app.shutdown()
    logger.info("PTB Application shut down")


app = FastAPI(title="news-bot-api", lifespan=lifespan)

_MAX_WEBHOOK_BODY_BYTES = 65_536  # 64 KiB — Telegram updates are much smaller in practice


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    """
    Receive and process Telegram webhook updates.
    Validates Telegram's secret token header to prevent spoofed calls.
    """
    if not secrets.compare_digest(
        x_telegram_bot_api_secret_token, settings.WEBHOOK_SECRET_TOKEN
    ):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")

    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")

    try:
        import json as _json
        update = Update.de_json(_json.loads(body), app.state.ptb_app.bot)
        await app.state.ptb_app.process_update(update)
    except Exception as exc:
        logger.error("Error processing webhook update: %s", exc, exc_info=True)
        # Return 200 to prevent Telegram from retrying a malformed update
        return {"ok": False, "error": "processing error"}

    return {"ok": True}


@app.get("/health")
async def health() -> dict:
    """Liveness probe for Cloud Run."""
    return {"status": "ok", "service": "api"}
