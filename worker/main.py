"""Worker FastAPI service — Cloud Scheduler calls POST /run every hour."""

import asyncio
import json
import logging
from datetime import datetime

import vertexai  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner  # type: ignore[import-untyped]
from google.adk.sessions import InMemorySessionService  # type: ignore[import-untyped]
from google.genai.types import Content, Part  # type: ignore[import-untyped]

import shared.database as db
from shared.config import settings
from shared.models import User
from worker.pipeline.root import root_agent
from worker.tools.telegram_tools import send_digest_message, send_error_to_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialise Vertex AI once at module import — worker only
vertexai.init(project=settings.GCP_PROJECT_ID, location=settings.VERTEX_AI_LOCATION)

# Module-level ADK singletons
session_service = InMemorySessionService()
adk_runner = Runner(
    agent=root_agent,
    app_name="news_bot",
    session_service=session_service,
)

app = FastAPI(title="news-bot-worker")


# ---------------------------------------------------------------------------
# Per-user pipeline
# ---------------------------------------------------------------------------


async def process_user(user: User) -> None:
    """Run the full ADK pipeline for a single user and deliver the digest."""
    try:
        session = await session_service.create_session(
            app_name="news_bot",
            user_id=user.telegram_id,
        )

        message = Content(
            role="user",
            parts=[Part(text=f"telegram_id: {user.telegram_id}")],
        )

        final_digest_json: str = "[]"
        async for event in adk_runner.run_async(
            user_id=user.telegram_id,
            session_id=session.id,
            new_message=message,
        ):
            if event.is_final_response():
                # Read output from session state — more reliable than parsing LLM text
                state = await session_service.get_session(
                    app_name="news_bot",
                    user_id=user.telegram_id,
                    session_id=session.id,
                )
                raw = state.state.get("final_digest", "[]")
                final_digest_json = raw if isinstance(raw, str) else json.dumps(raw)
                break

        await send_digest_message(user.telegram_id, final_digest_json)
        await db.update_user(
            user.telegram_id,
            last_digest_sent=datetime.utcnow(),
            total_digests_sent=user.total_digests_sent + 1,
        )
        logger.info("Digest delivered to user %s", user.telegram_id)
    except Exception as exc:
        logger.error("process_user(%s) failed: %s", user.telegram_id, exc, exc_info=True)
        try:
            await send_error_to_user(
                user.telegram_id,
                "Sorry, we encountered an error generating your digest. We'll try again next hour.",
            )
        except Exception as notify_exc:
            logger.error(
                "Failed to notify user %s of error: %s", user.telegram_id, notify_exc
            )
        raise


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/run")
async def run_digests(request: Request) -> dict:
    """
    Trigger hourly digest delivery.
    Authentication is handled by Cloud Run IAM (OIDC via Cloud Scheduler).
    """
    utc_hour = datetime.utcnow().hour
    try:
        users = await db.get_active_users_for_hour(utc_hour)
    except Exception as exc:
        logger.error("Failed to fetch users for hour %d: %s", utc_hour, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch users")

    if not users:
        logger.info("No users scheduled for UTC hour %d", utc_hour)
        return {"users_processed": 0, "errors": 0}

    results = await asyncio.gather(
        *[process_user(u) for u in users], return_exceptions=True
    )

    error_count = sum(1 for r in results if isinstance(r, BaseException))
    logger.info(
        "Run complete: %d users, %d errors (UTC hour %d)",
        len(users),
        error_count,
        utc_hour,
    )
    return {"users_processed": len(users), "errors": error_count}


@app.get("/health")
async def health() -> dict:
    """Liveness probe for Cloud Run."""
    return {"status": "ok", "service": "worker"}
