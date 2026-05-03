"""Worker FastAPI service — Cloud Scheduler calls POST /run every hour."""

import asyncio
import json
import re
from datetime import datetime

import vertexai  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner  # type: ignore[import-untyped]
from google.adk.sessions import InMemorySessionService  # type: ignore[import-untyped]
from google.genai.types import Content, Part  # type: ignore[import-untyped]

import shared.database as db
from shared.config import settings
from shared.models import User
from utils.logging import get_logger, setup_logging
from worker.pipeline.curator import curator_agent
from worker.pipeline.fetcher import fetch_articles_for_user
from worker.pipeline.summariser import summariser_agent
from worker.tools.firestore_tools import get_user_preferences
from worker.tools.telegram_tools import send_digest_message, send_error_to_user

setup_logging()
logger = get_logger(__name__)

# Initialise Vertex AI once at module import — worker only
vertexai.init(project=settings.GCP_PROJECT_ID, location=settings.VERTEX_AI_LOCATION)

# Module-level ADK singletons — two separate runners share one session store
session_service = InMemorySessionService()
curator_runner = Runner(
    agent=curator_agent,
    app_name="news_bot",
    session_service=session_service,
)
summariser_runner = Runner(
    agent=summariser_agent,
    app_name="news_bot",
    session_service=session_service,
)

app = FastAPI(title="news-bot-worker")


def _normalize_articles(raw: object) -> list[dict]:
    """Coerce LLM output to list[dict], handling double-encoding and mixed element types."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    result.append(parsed)
            except (json.JSONDecodeError, ValueError):
                pass
    return result


def _is_safe_article_url(url: object) -> bool:
    """Return True for HTTP(S) article URLs we can safely hand to Telegram."""
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _weighted_topic_selection(
    articles: list[dict],
    topic_weights: dict[str, float],
    total_slots: int = settings.DIGEST_MAX_ARTICLES,
) -> list[dict]:
    """Allocate final digest slots proportional to topic weights.

    Each eligible topic (one that has curated articles) receives at least 1 slot.
    Slots scale with weight so higher-weight topics appear more often.
    """
    by_topic: dict[str, list[dict]] = {}
    for a in articles:
        by_topic.setdefault(a.get("topic", ""), []).append(a)

    eligible = [t for t in topic_weights if by_topic.get(t)]
    if not eligible:
        return articles[:total_slots]

    total_w = sum(topic_weights.get(t, 1.0) for t in eligible)
    slots = {
        t: max(1, round(topic_weights.get(t, 1.0) / total_w * total_slots))
        for t in eligible
    }

    result: list[dict] = []
    for t in sorted(eligible, key=lambda x: topic_weights.get(x, 1.0), reverse=True):
        ranked = sorted(
            by_topic[t],
            key=lambda a: a.get("relevance_score", 0.0),
            reverse=True,
        )
        result.extend(ranked[: slots[t]])

    return result[:total_slots]


def _inject_digest_assets(
    articles: list[dict],
    url_map: dict[str, str],
    image_map: dict[str, str],
    citation_status_map: dict[str, str] | None = None,
    published_at_map: dict[str, str] | None = None,
    user_id: str = "",
) -> list[dict]:
    """Inject real URLs/images/dates saved by search_news into final digest articles."""
    citation_status_map = citation_status_map or {}
    published_at_map = published_at_map or {}
    logger.info(
        "URL injection source: %d URLs, %d images, %d citation statuses, %d dates for user %s",
        len(url_map),
        len(image_map),
        len(citation_status_map),
        len(published_at_map),
        user_id,
    )
    url_matches = 0
    for article in articles:
        aid = article.get("article_id", "")
        title = article.get("title", "")
        injected_url = url_map.get(aid) or url_map.get(title)
        injected_image = image_map.get(aid) or image_map.get(title)
        citation_status = citation_status_map.get(aid) or citation_status_map.get(title)
        injected_pub_date = published_at_map.get(aid)
        if injected_url:
            article["url"] = injected_url
            url_matches += 1
        elif not article.get("url"):
            logger.warning(
                "No URL match for article_id=%r title=%r user=%s",
                aid,
                title,
                user_id,
            )
        if injected_image:
            article["image_url"] = injected_image
        if citation_status:
            article["citation_status"] = citation_status
        if injected_pub_date and not article.get("published_at"):
            article["published_at"] = injected_pub_date
            logger.info(
                "Injected published_at=%r for article_id=%r user=%s",
                injected_pub_date,
                aid,
                user_id,
            )
        elif not article.get("published_at") and not injected_pub_date:
            logger.warning(
                "No published_at match for article_id=%r title=%r user=%s",
                aid,
                title,
                user_id,
            )
    logger.info(
        "Matched %d final digest URLs by article_id/title for user %s",
        url_matches,
        user_id,
    )
    return articles


def _filter_deliverable_articles(articles: list[dict]) -> list[dict]:
    """Drop articles with confirmed broken or malformed citations before delivery."""
    deliverable = []
    for article in articles:
        url = article.get("url", "")
        citation_status = article.get("citation_status", "")
        if citation_status == "broken" or not _is_safe_article_url(url):
            reason = "broken citation" if citation_status == "broken" else "no valid URL"
            logger.warning(
                "Dropping article (%s): article_id=%r title=%r url=%r status=%r",
                reason,
                article.get("article_id", ""),
                article.get("title", ""),
                url,
                citation_status,
            )
            continue
        deliverable.append(article)
    logger.info("Kept %d/%d articles after citation filtering", len(deliverable), len(articles))
    return deliverable


def _clean_digest_urls(articles: list[dict]) -> list[dict]:
    """Backward-compatible wrapper for tests and older call sites."""
    return _filter_deliverable_articles(articles)


# ---------------------------------------------------------------------------
# Per-user pipeline
# ---------------------------------------------------------------------------


async def process_user(user: User) -> None:
    """Run the full pipeline for a single user and deliver the digest."""
    try:
        prefs = await get_user_preferences(user.telegram_id)

        # 1. Deterministic fetch — Python controls every query string
        prefetch_state: dict = {"user_preferences": prefs}
        await fetch_articles_for_user(prefs, prefetch_state)

        # 2. Session pre-populated with raw_articles + URL/image/citation/date maps
        session = await session_service.create_session(
            app_name="news_bot",
            user_id=user.telegram_id,
            state=prefetch_state,
        )

        trigger = Content(
            role="user",
            parts=[Part(text=f"telegram_id: {user.telegram_id}")],
        )

        # 3. Curator pass — scores and deduplicates raw_articles → curated_articles
        async for _ in curator_runner.run_async(
            user_id=user.telegram_id,
            session_id=session.id,
            new_message=trigger,
        ):
            pass

        # 4. Weighted selection — limit curated_articles to per-topic slot quota
        #    before summariser so we don't waste tokens on articles we'll drop
        live = await session_service.get_session(
            app_name="news_bot",
            user_id=user.telegram_id,
            session_id=session.id,
        )
        raw_curated = live.state.get("curated_articles", "[]")
        if isinstance(raw_curated, str):
            raw_curated = re.sub(r"^```(?:json)?\s*", "", raw_curated.strip())
            raw_curated = re.sub(r"\s*```$", "", raw_curated.strip())
            if not raw_curated.strip():
                logger.warning("Curator returned empty output for user %s — skipping", user.telegram_id)
                parsed_curated: list = []
            else:
                try:
                    parsed_curated = json.loads(raw_curated)
                except (json.JSONDecodeError, ValueError):
                    m = re.search(r"\[[\s\S]*\]", raw_curated)
                    parsed_curated = json.loads(m.group(0)) if m else []
        else:
            parsed_curated = raw_curated or []
        curated = _normalize_articles(parsed_curated)
        weighted = _weighted_topic_selection(
            curated, prefs.get("topic_weights", {}), total_slots=settings.DIGEST_MAX_ARTICLES
        )
        logger.info(
            "Weighted selection: %d curated → %d selected for user %s",
            len(curated),
            len(weighted),
            user.telegram_id,
        )

        # 5. Summariser pass — weighted subset injected via state_delta (ADK-native, persists to session)
        async for _ in summariser_runner.run_async(
            user_id=user.telegram_id,
            session_id=session.id,
            new_message=trigger,
            state_delta={"curated_articles": json.dumps(weighted)},
        ):
            pass

        # 6. Read final_digest from session state
        state = await session_service.get_session(
            app_name="news_bot",
            user_id=user.telegram_id,
            session_id=session.id,
        )
        logger.info("session state keys: %s", list(state.state.keys()))
        raw = state.state.get("final_digest", "[]")
        if isinstance(raw, str):
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$", "", raw.strip())
            try:
                json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                m = re.search(r"\[[\s\S]*\]", raw)
                if m:
                    raw = m.group(0)
        final_digest_json: str = raw if isinstance(raw, str) else json.dumps(raw)

        # Final hard cap — guards against the LLM expanding input beyond the weighted subset
        try:
            _cap_articles = _normalize_articles(json.loads(final_digest_json))
            if len(_cap_articles) > settings.DIGEST_MAX_ARTICLES:
                logger.info(
                    "Final digest capped: %d → %d for user %s",
                    len(_cap_articles), settings.DIGEST_MAX_ARTICLES, user.telegram_id,
                )
                final_digest_json = json.dumps(_cap_articles[:settings.DIGEST_MAX_ARTICLES])
        except (json.JSONDecodeError, TypeError):
            pass

        # 7. Inject real URLs, images, and dates from session state
        url_map: dict[str, str] = dict(state.state.get("url_map") or {})
        image_map: dict[str, str] = dict(state.state.get("image_map") or {})
        citation_status_map: dict[str, str] = dict(state.state.get("citation_status_map") or {})
        published_at_map: dict[str, str] = dict(state.state.get("published_at_map") or {})
        if url_map or image_map or citation_status_map or published_at_map:
            try:
                articles = _normalize_articles(json.loads(final_digest_json))
                articles = _inject_digest_assets(
                    articles,
                    url_map,
                    image_map,
                    citation_status_map,
                    published_at_map,
                    user.telegram_id,
                )
                final_digest_json = json.dumps(articles)
                injected = sum(1 for a in articles if a.get("url"))
                logger.info("Injected %d real URLs for user %s", injected, user.telegram_id)
            except (json.JSONDecodeError, TypeError):
                pass

        # 8. Drop articles whose citations are malformed or confirmed broken
        try:
            articles = _normalize_articles(json.loads(final_digest_json))
            articles = _filter_deliverable_articles(articles)
            final_digest_json = json.dumps(articles)
        except (json.JSONDecodeError, TypeError):
            pass

        logger.info("final_digest for user %s: %s", user.telegram_id, final_digest_json[:500])
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
