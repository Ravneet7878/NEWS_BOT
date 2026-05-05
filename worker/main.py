"""Worker FastAPI service — Cloud Scheduler calls POST /run every hour."""

import asyncio
import hashlib
import json
import re
from datetime import timedelta

import httpx
import vertexai  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner  # type: ignore[import-untyped]
from google.adk.sessions import InMemorySessionService  # type: ignore[import-untyped]
from google.api_core import exceptions as _gapi_exc  # type: ignore[import-untyped]
from google.genai.types import Content, Part  # type: ignore[import-untyped]
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

import shared.database as db
from shared.config import settings
from shared.models import User
from utils.guardrails import validate_curated_articles, validate_final_digest
from utils.logging import get_logger, setup_logging
from utils.privacy import public_user_ref
from utils.time import utc_now
from worker.pipeline.curator import curator_agent
from worker.pipeline.fetcher import fetch_articles_for_user
from worker.pipeline import llm_cache
from worker.pipeline.prefetch import TopicResult, prefetch_topic_news
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


def _is_transient_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, (
        _gapi_exc.ResourceExhausted,
        _gapi_exc.ServiceUnavailable,
        _gapi_exc.DeadlineExceeded,
        _gapi_exc.InternalServerError,
    )):
        return True
    try:
        import grpc  # type: ignore[import-untyped]
        if isinstance(exc, grpc.RpcError):
            return exc.code() in (  # type: ignore[union-attr]
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.INTERNAL,
            )
    except ImportError:
        pass
    return False


async def _with_llm_retry(fn):
    """Run async fn() with exponential backoff on transient LLM errors."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(settings.RETRY_MAX_ATTEMPTS),
        wait=wait_random_exponential(
            multiplier=settings.RETRY_BACKOFF_BASE_SECONDS,
            max=settings.RETRY_BACKOFF_MAX_SECONDS,
        ),
        retry=retry_if_exception(_is_transient_llm_error),
        reraise=True,
    ):
        with attempt:
            return await fn()


def _fix_invalid_unicode_escapes(s: str) -> str:
    """Escape bare \\u not followed by 4 hex digits so json.loads won't reject them."""
    return re.sub(r"\\u(?![0-9a-fA-F]{4})", r"\\\\u", s)


def _parse_llm_json(raw: str) -> list:
    """Strip markdown fences, fix bad unicode escapes, then parse LLM JSON to a list.

    Always returns a list. If the top-level parse produces a non-list (e.g. the model
    wrapped the array in a JSON object), falls through to regex array extraction.
    """
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    if not raw.strip():
        return []
    for candidate in (raw, _fix_invalid_unicode_escapes(raw)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
            # Non-list (e.g. dict wrapper) — fall through to regex extraction
        except (json.JSONDecodeError, ValueError):
            pass
        m = re.search(r"\[[\s\S]*\]", candidate)
        if m:
            try:
                result = json.loads(m.group(0))
                if isinstance(result, list):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass
    return []


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


async def summarise_in_batches(weighted: list[dict], user_ref: str) -> list[dict]:
    """Summarise articles in parallel batches, one LLM call per article within a batch.

    Each batch creates a fresh ADK session so per-article summariser writes don't
    race on the same `final_digest` key. Articles within a batch are summarised
    sequentially (one article in the prompt at a time → no cross-article hallucination).
    Batches run in parallel, capped by SUMMARISER_CONCURRENCY.
    """
    if not weighted:
        return []

    batch_size = max(1, settings.SUMMARISER_BATCH_SIZE)
    batches: list[list[dict]] = [
        weighted[i : i + batch_size] for i in range(0, len(weighted), batch_size)
    ]
    sem = asyncio.Semaphore(settings.SUMMARISER_CONCURRENCY)

    async def _summarise_one(article: dict, batch_session_id: str) -> list[dict]:
        async for _ in summariser_runner.run_async(
            user_id=user_ref,
            session_id=batch_session_id,
            new_message=Content(role="user", parts=[Part(text=f"user_ref: {user_ref}")]),
            state_delta={"curated_articles": json.dumps([article])},
        ):
            pass
        live = await session_service.get_session(
            app_name="news_bot",
            user_id=user_ref,
            session_id=batch_session_id,
        )
        raw = live.state.get("final_digest", "[]")
        parsed = _parse_llm_json(raw) if isinstance(raw, str) else (raw or [])
        return _normalize_articles(parsed)

    async def _run_batch(batch: list[dict], idx: int) -> list[dict]:
        async with sem:
            try:
                out: list[dict] = []
                for article in batch:
                    # Fresh session per article — reusing a session across articles causes the
                    # model to see prior turns and re-emit earlier summaries in final_digest.
                    article_session = await session_service.create_session(
                        app_name="news_bot",
                        user_id=user_ref,
                        state={},
                    )
                    # _run_batch's except catches Tenacity reraise — batch degrades to [] on exhaustion
                    out.extend(await _with_llm_retry(
                        lambda a=article, sid=article_session.id: _summarise_one(a, sid)
                    ))
                return out
            except Exception as exc:
                logger.error(
                    "summarise_in_batches: batch %d failed for user %s: %s",
                    idx,
                    user_ref,
                    exc,
                    exc_info=True,
                )
                return []

    results = await asyncio.gather(
        *[_run_batch(b, i) for i, b in enumerate(batches)]
    )
    merged: list[dict] = []
    for r in results:
        merged.extend(r)
    logger.info(
        "summarise_in_batches: user=%s batches=%d articles_in=%d articles_out=%d",
        user_ref,
        len(batches),
        len(weighted),
        len(merged),
    )
    return merged


async def process_user(user: User, prefetched: dict[str, TopicResult]) -> None:
    """Run the full pipeline for a single user and deliver the digest."""
    try:
        _now = utc_now()
        _today = _now.date()
        _utc_hour = _now.hour
        _utc_minute = _now.minute
        _pending = await db.get_pending_digest(user.telegram_id, _today, _utc_hour, _utc_minute)
        if _pending:
            _pending_articles = _pending.get("articles") or []
            if _pending.get("delivered_at") is not None and _pending_articles:
                logger.info(
                    "process_user: digest already delivered for user %s %02d:%02d — skipping",
                    public_user_ref(user.telegram_id), _utc_hour, _utc_minute,
                )
                return
            if _pending_articles:
                logger.info(
                    "process_user: pending digest HIT for user %s %02d:%02d — skipping LLM pipeline",
                    public_user_ref(user.telegram_id), _utc_hour, _utc_minute,
                )
                await send_digest_message(user.telegram_id, json.dumps(_pending_articles))
                await db.mark_pending_delivered(user.telegram_id, _today, _utc_hour, _utc_minute)
                await db.update_user(
                    user.telegram_id,
                    last_digest_sent=utc_now(),
                    total_digests_sent=user.total_digests_sent + 1,
                )
                return

        prefs = await get_user_preferences(user.telegram_id)

        # 1. Per-user assembly from prefetched topic data
        prefetch_state: dict = {"user_preferences": prefs}
        await fetch_articles_for_user(prefs, prefetch_state, prefetched)

        # Build source-of-truth index from fetcher output (used by validation gates)
        raw_articles_list = _normalize_articles(
            json.loads(prefetch_state.get("raw_articles", "[]"))
        )
        raw_articles_by_id: dict[str, dict] = {
            a["article_id"]: a for a in raw_articles_list if a.get("article_id")
        }
        valid_ids: set[str] = set(raw_articles_by_id.keys())

        # 2. Session pre-populated with raw_articles + URL/image/citation/date maps
        _user_ref = public_user_ref(user.telegram_id)
        session = await session_service.create_session(
            app_name="news_bot",
            user_id=_user_ref,
            state=prefetch_state,
        )

        trigger = Content(
            role="user",
            parts=[Part(text=f"user_ref: {public_user_ref(user.telegram_id)}")],
        )

        # 3. Curator pass — scores and deduplicates raw_articles → curated_articles
        async def _run_curator() -> list:
            async for _ in curator_runner.run_async(
                user_id=_user_ref,
                session_id=session.id,
                new_message=trigger,
            ):
                pass
            live = await session_service.get_session(
                app_name="news_bot",
                user_id=_user_ref,
                session_id=session.id,
            )
            raw_curated = live.state.get("curated_articles", "[]")
            if isinstance(raw_curated, str):
                return _parse_llm_json(raw_curated)
            return raw_curated or []

        parsed_curated = await _with_llm_retry(_run_curator)
        # One retry if curator produced nothing (or a non-list) despite non-empty raw_articles
        if (not parsed_curated or not isinstance(parsed_curated, list)) and raw_articles_list:
            logger.warning(
                "Curator returned empty output for user %s — retrying once",
                public_user_ref(user.telegram_id),
            )
            parsed_curated = await _with_llm_retry(_run_curator)
        if not parsed_curated or not isinstance(parsed_curated, list):
            logger.warning(
                "Curator still empty after retry for user %s — falling back to raw articles",
                public_user_ref(user.telegram_id),
            )
            sorted_raw = sorted(raw_articles_list, key=lambda a: a.get("published_at", ""), reverse=True)
            parsed_curated = [
                {
                    "article_id": a.get("article_id", ""),
                    "title": a.get("title", ""),
                    "url": "",
                    "summary": a.get("snippet", a.get("summary", "")),
                    "topic": a.get("topic", ""),
                    "relevance_score": 0.5,
                    "source": a.get("source", a.get("source_name", "")),
                    "published_at": a.get("published_at", a.get("pub_date", "")),
                }
                for a in sorted_raw[: settings.DIGEST_MAX_ARTICLES * 2]
            ]
        curated = _normalize_articles(parsed_curated)
        curated = validate_curated_articles(curated, valid_ids, prefs.get("topics", []))
        logger.info("Curated articles after validation: %d for user %s", len(curated), _user_ref)
        weighted = _weighted_topic_selection(
            curated, prefs.get("topic_weights", {}), total_slots=settings.DIGEST_MAX_ARTICLES
        )
        logger.info(
            "Weighted selection: %d curated → %d selected for user %s",
            len(curated),
            len(weighted),
            public_user_ref(user.telegram_id),
        )

        # 5. Batched parallel summarisation — 1 article per LLM call, batches run in parallel
        parsed_final = await summarise_in_batches(weighted, _user_ref)

        # 6. Re-read original session for url_map / image_map / etc
        state = await session_service.get_session(
            app_name="news_bot",
            user_id=_user_ref,
            session_id=session.id,
        )
        logger.info("session state keys for user %s: %s", _user_ref, list(state.state.keys()))
        final_digest_json: str = json.dumps(parsed_final)

        # Validate summariser output against source of truth, then cap
        logger.info("parsed_final before guardrails: %d articles for user %s", len(_normalize_articles(parsed_final)), _user_ref)
        _validated = validate_final_digest(
            _normalize_articles(parsed_final), valid_ids, prefs.get("topics", []), raw_articles_by_id
        )
        if len(_validated) > settings.DIGEST_MAX_ARTICLES:
            logger.info(
                "Final digest capped: %d → %d for user %s",
                len(_validated), settings.DIGEST_MAX_ARTICLES, public_user_ref(user.telegram_id),
            )
            _validated = _validated[:settings.DIGEST_MAX_ARTICLES]
        final_digest_json = json.dumps(_validated)

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
                    public_user_ref(user.telegram_id),
                )
                final_digest_json = json.dumps(articles)
                injected = sum(1 for a in articles if a.get("url"))
                logger.info("Injected %d real URLs for user %s", injected, public_user_ref(user.telegram_id))
            except (json.JSONDecodeError, TypeError):
                pass

        # 8. Drop articles whose citations are malformed or confirmed broken
        try:
            articles = _normalize_articles(json.loads(final_digest_json))
            articles = _filter_deliverable_articles(articles)
            final_digest_json = json.dumps(articles)
        except (json.JSONDecodeError, TypeError):
            pass

        try:
            _article_count = len(_normalize_articles(json.loads(final_digest_json)))
        except (json.JSONDecodeError, TypeError):
            _article_count = 0
        logger.info(
            "final_digest for user %s: %d articles, %d bytes",
            public_user_ref(user.telegram_id),
            _article_count,
            len(final_digest_json),
        )
        if _article_count == 0:
            logger.warning(
                "process_user: empty final digest for user %s — skipping delivery this hour",
                public_user_ref(user.telegram_id),
            )
            return
        await send_digest_message(user.telegram_id, final_digest_json)
        try:
            _final_articles = _normalize_articles(json.loads(final_digest_json))
            await db.save_pending_digest(user.telegram_id, _today, _utc_hour, _final_articles)
            await db.mark_pending_delivered(user.telegram_id, _today, _utc_hour)
        except Exception as _cache_exc:
            logger.warning(
                "Failed to cache pending digest for user %s: %s",
                public_user_ref(user.telegram_id), _cache_exc,
            )
        sent_at = utc_now()
        await db.update_user(
            user.telegram_id,
            last_digest_sent=sent_at,
            total_digests_sent=user.total_digests_sent + 1,
        )
        try:
            delivered = _normalize_articles(json.loads(final_digest_json))
            await db.save_user_digest_history(user.telegram_id, delivered, sent_at)
        except Exception as hist_exc:
            logger.error(
                "save_user_digest_history(%s) failed: %s",
                public_user_ref(user.telegram_id),
                hist_exc,
                exc_info=True,
            )
        logger.info("Digest delivered to user %s", public_user_ref(user.telegram_id))
    except Exception as exc:
        logger.error("process_user(%s) failed: %s", public_user_ref(user.telegram_id), exc, exc_info=True)
        try:
            await send_error_to_user(
                user.telegram_id,
                "Sorry, we encountered an error generating your digest. We'll try again next hour.",
            )
        except Exception as notify_exc:
            logger.error(
                "Failed to notify user %s of error: %s", public_user_ref(user.telegram_id), notify_exc
            )
        raise


# ---------------------------------------------------------------------------
# /prepare helpers — topic-level curator + article-level summariser
# ---------------------------------------------------------------------------


async def run_curator_for_topic(topic: str, topic_result: TopicResult) -> list[dict]:
    """Run the curator LLM once for a single topic. Returns curated articles with URLs injected."""
    url_map: dict[str, str] = {}
    image_map: dict[str, str] = {}
    citation_status_map: dict[str, str] = {}
    published_at_map: dict[str, str] = {}
    raw_articles_list: list[dict] = []

    for a in topic_result.raw_articles:
        article_id = hashlib.sha256(a["link"].encode()).hexdigest()[:16]
        url_map[article_id] = a["link"]
        citation_status_map[article_id] = a["citation_status"]
        published_at_map[article_id] = a["pub_date"]
        if a["image_url"]:
            image_map[article_id] = a["image_url"]
        raw_articles_list.append({
            "article_id": article_id,
            "title": a["title"],
            "source": a["source_name"],
            "snippet": a["snippet"],
            "published_at": a["pub_date"],
            "topic": topic,
            "url": "",
            "summary": a["snippet"],
        })

    if not raw_articles_list:
        return []

    valid_ids = set(url_map.keys())
    user_ref = f"prepare_{hashlib.sha256(topic.encode()).hexdigest()[:8]}"
    session = await session_service.create_session(
        app_name="news_bot",
        user_id=user_ref,
        state={"raw_articles": json.dumps(raw_articles_list)},
    )

    async def _run() -> list[dict]:
        async for _ in curator_runner.run_async(
            user_id=user_ref,
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=f"user_ref: {user_ref}")]),
        ):
            pass
        live = await session_service.get_session(
            app_name="news_bot", user_id=user_ref, session_id=session.id
        )
        raw = live.state.get("curated_articles", "[]")
        parsed = _parse_llm_json(raw) if isinstance(raw, str) else (raw or [])
        return _normalize_articles(parsed)

    curated = await _with_llm_retry(_run)
    if not curated and raw_articles_list:
        logger.warning("run_curator_for_topic(%r): empty output — retrying once", topic)
        curated = await _with_llm_retry(_run)

    curated = validate_curated_articles(curated, valid_ids, [topic])

    # Inject real URLs, images, and dates back into the curated articles
    for art in curated:
        aid = art.get("article_id", "")
        if url_map.get(aid):
            art["url"] = url_map[aid]
        if citation_status_map.get(aid):
            art["citation_status"] = citation_status_map[aid]
        if image_map.get(aid):
            art["image_url"] = image_map[aid]
        if published_at_map.get(aid) and not art.get("published_at"):
            art["published_at"] = published_at_map[aid]

    # Drop articles whose URLs didn't survive injection
    curated = [a for a in curated if _is_safe_article_url(a.get("url", ""))]

    logger.info("run_curator_for_topic(%r): %d articles after curation", topic, len(curated))
    return curated


async def run_summariser_for_article(art: dict) -> dict:
    """Run the summariser LLM for a single article. Returns the summary dict."""
    user_ref = "prepare_summarise"

    async def _call() -> dict:
        # Each retry creates a new session; InMemorySessionService accepts the accumulation.
        session = await session_service.create_session(
            app_name="news_bot",
            user_id=user_ref,
            state={},
        )
        async for _ in summariser_runner.run_async(
            user_id=user_ref,
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=f"user_ref: {user_ref}")]),
            state_delta={"curated_articles": json.dumps([art])},
        ):
            pass
        live = await session_service.get_session(
            app_name="news_bot", user_id=user_ref, session_id=session.id
        )
        raw = live.state.get("final_digest", "[]")
        parsed = _parse_llm_json(raw) if isinstance(raw, str) else (raw or [])
        results = _normalize_articles(parsed)
        return results[0] if results else {}

    return await _with_llm_retry(_call)


def _flatten_curated_for_user(
    curated_by_topic: dict[str, list[dict]],
    topics: list[str] | None,
) -> list[dict]:
    """Merge curated articles from the user's topics into a single flat list."""
    result: list[dict] = []
    seen_urls: set[str] = set()
    for topic in (topics or []):
        for art in curated_by_topic.get(topic, []):
            url = art.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            result.append(art)
    return result


def _dedup_articles_by_url(
    per_user_selections: dict[str, list[dict]],
) -> list[dict]:
    """Return one article per unique URL across all users' selections."""
    seen: dict[str, dict] = {}
    for articles in per_user_selections.values():
        for art in articles:
            url = art.get("url", "")
            if url and url not in seen:
                seen[url] = art
    return list(seen.values())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/prepare")
async def prepare_digests(request: Request) -> dict:
    """Pre-build digests for users whose delivery slot falls in the next PREPARE_BUFFER_MINUTES window.

    Per-minute cadence: runs every minute. Builds a forward-looking slot list, fetches users
    whose (hour, minute) matches any slot, skips those with an existing pending doc, and builds
    the rest. Idempotent under overlapping invocations.
    """
    now = utc_now()
    window_end = now + timedelta(minutes=settings.PREPARE_BUFFER_MINUTES)
    cursor = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)

    slots: list[tuple] = []
    while cursor <= window_end:
        slots.append((cursor.date(), cursor.hour, cursor.minute))
        cursor += timedelta(minutes=1)

    hours = sorted({h for _, h, _ in slots})
    slot_lookup: dict[tuple[int, int], "date"] = {(h, m): d for d, h, m in slots}

    try:
        candidates = await db.get_active_users_for_window(hours)
    except Exception as exc:
        logger.error("prepare_digests: failed to fetch users: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch users")

    # Filter to users whose slot is in the window and don't already have a pending doc
    to_prepare: list[tuple] = []
    for u in candidates:
        key = (u.delivery_hour_utc, u.delivery_minute_utc)
        if key not in slot_lookup:
            continue
        target_date = slot_lookup[key]
        existing = await db.get_pending_digest(u.telegram_id, target_date, key[0], key[1])
        if existing is not None:
            continue
        to_prepare.append((u, target_date, key[0], key[1]))

    if not to_prepare:
        logger.info("prepare_digests: no users to prepare in window ending %s", window_end.isoformat())
        return {"prepared": 0}

    users_to_prepare = [t[0] for t in to_prepare]
    unique_topics: set[str] = {t for u in users_to_prepare for t in (u.topics or [])}
    logger.info(
        "prepare_digests: %d users, %d unique topics, window=%s–%s",
        len(to_prepare), len(unique_topics), now.isoformat(), window_end.isoformat(),
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        follow_redirects=True,
    ) as client:
        prefetched = await prefetch_topic_news(unique_topics, client)

    # Step 1: curator — once per topic, sequential, Firestore-cached
    curated_by_topic: dict[str, list[dict]] = {}
    for topic in unique_topics:
        hit = await llm_cache.get_cached_curated_topic(topic)
        if hit is not None:
            curated_by_topic[topic] = hit
        else:
            topic_result = prefetched.get(topic)
            if topic_result is None:
                logger.warning("prepare_digests: no prefetch result for topic=%r", topic)
                curated_by_topic[topic] = []
                continue
            curated = await run_curator_for_topic(topic, topic_result)
            await llm_cache.set_cached_curated_topic(topic, curated)
            curated_by_topic[topic] = curated

    # Step 2: per-user weighted selection (pure Python, no LLM)
    per_user_selections: dict[str, list[dict]] = {}
    for user, _td, _th, _tm in to_prepare:
        articles = _flatten_curated_for_user(curated_by_topic, user.topics)
        selected = _weighted_topic_selection(articles, user.topic_weights or {})
        per_user_selections[user.telegram_id] = selected

    # Step 3: summariser — once per unique URL, sequential, Firestore-cached
    unique_articles = _dedup_articles_by_url(per_user_selections)
    summary_by_url: dict[str, dict] = {}
    for art in unique_articles:
        url = art.get("url", "")
        if not url:
            continue
        hit = await llm_cache.get_cached_article_summary(url)
        if hit is not None:
            summary_by_url[url] = hit
        else:
            summary = await run_summariser_for_article(art)
            if summary:
                await llm_cache.set_cached_article_summary(url, summary)
                summary_by_url[url] = summary

    # Step 4: assemble and store pending digest per user
    for user, target_date, target_hour, target_minute in to_prepare:
        digest_articles = [
            {**art, **summary_by_url[art["url"]]}
            for art in per_user_selections[user.telegram_id]
            if art.get("url") in summary_by_url
        ]
        digest_articles = _filter_deliverable_articles(digest_articles)
        if not digest_articles:
            logger.warning(
                "prepare_digests: empty digest for %s — skipping save",
                public_user_ref(user.telegram_id),
            )
            continue
        try:
            await db.save_pending_digest(
                user.telegram_id, target_date, target_hour, digest_articles, target_minute
            )
        except Exception as exc:
            logger.error(
                "prepare_digests: save_pending_digest failed for %s: %s",
                public_user_ref(user.telegram_id), exc, exc_info=True,
            )

    logger.info(
        "prepare_digests complete: prepared=%d topics=%d unique_articles=%d",
        len(to_prepare), len(unique_topics), len(unique_articles),
    )
    return {
        "prepared": len(to_prepare),
        "topics": len(unique_topics),
        "unique_articles_summarised": len(unique_articles),
    }


@app.post("/deliver")
async def deliver_digests(request: Request) -> dict:
    """Deliver pre-built digests from pending_digests. No LLM calls.

    If /prepare has not produced a pending digest yet, return a retryable 503 so
    Cloud Scheduler can retry. Use /run for manual live recovery.
    """
    now = utc_now()
    target_hour = now.hour
    target_minute = now.minute
    target_date = now.date()

    try:
        all_hour_users = await db.get_active_users_for_hour(target_hour)
    except Exception as exc:
        logger.error("deliver_digests: failed to fetch users: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch users")

    users = [u for u in all_hour_users if u.delivery_minute_utc == target_minute]

    if not users:
        logger.info("deliver_digests: no users for UTC %02d:%02d", target_hour, target_minute)
        return {
            "users_total": 0,
            "delivered": 0,
            "already_delivered": 0,
            "skipped_empty": 0,
            "missing_pending": 0,
            "errors": 0,
        }

    sem = asyncio.Semaphore(settings.USER_PROCESS_CONCURRENCY)

    async def _deliver_one(user: User) -> str:
        async with sem:
            pending = await db.get_pending_digest(user.telegram_id, target_date, target_hour, target_minute)
            if pending is None:
                logger.warning(
                    "deliver_digests: no pending digest for %s — waiting for Scheduler retry",
                    public_user_ref(user.telegram_id),
                )
                return "missing_pending"

            if pending.get("delivered_at") is not None:
                logger.info(
                    "deliver_digests: digest already delivered for %s — skipping resend",
                    public_user_ref(user.telegram_id),
                )
                return "already_delivered"

            articles_json = json.dumps(pending.get("articles", []))
            # Raises on Telegram failure — Firestore writes only happen after confirmed delivery.
            result = await send_digest_message(user.telegram_id, articles_json)
            if result.get("messages_sent", 0) == 0:
                logger.warning(
                    "deliver_digests: 0 messages sent for %s (empty digest) — skipping state update",
                    public_user_ref(user.telegram_id),
                )
                return "skipped_empty"
            sent_at = utc_now()
            await db.mark_pending_delivered(user.telegram_id, target_date, target_hour, target_minute)
            await db.update_user(
                user.telegram_id,
                last_digest_sent=sent_at,
                total_digests_sent=user.total_digests_sent + 1,
            )
            try:
                await db.save_user_digest_history(
                    user.telegram_id, pending.get("articles", []), sent_at
                )
            except Exception as hist_exc:
                logger.error(
                    "deliver_digests: save_user_digest_history(%s) failed: %s",
                    public_user_ref(user.telegram_id), hist_exc, exc_info=True,
                )
            logger.info("deliver_digests: delivered to %s", public_user_ref(user.telegram_id))
            return "delivered"

    results = await asyncio.gather(*[_deliver_one(u) for u in users], return_exceptions=True)
    stats = {
        "users_total": len(users),
        "delivered": 0,
        "already_delivered": 0,
        "skipped_empty": 0,
        "missing_pending": 0,
        "errors": 0,
    }
    for result in results:
        if isinstance(result, BaseException):
            stats["errors"] += 1
            continue
        if result in stats:
            stats[result] += 1
        else:
            stats["errors"] += 1

    logger.info(
        (
            "deliver_digests complete: users=%d delivered=%d already_delivered=%d "
            "skipped_empty=%d missing_pending=%d errors=%d (UTC %02d:%02d)"
        ),
        stats["users_total"],
        stats["delivered"],
        stats["already_delivered"],
        stats["skipped_empty"],
        stats["missing_pending"],
        stats["errors"],
        target_hour,
        target_minute,
    )
    if stats["missing_pending"] > 0:
        raise HTTPException(status_code=503, detail=stats)
    if stats["errors"] > 0:
        raise HTTPException(
            status_code=500,
            detail=stats,
        )
    return stats


@app.post("/run")
async def run_digests(request: Request) -> dict:
    """
    Trigger hourly digest delivery.
    Authentication is handled by Cloud Run IAM (OIDC via Cloud Scheduler).
    """
    _run_now = utc_now()
    utc_hour = _run_now.hour
    utc_minute = _run_now.minute
    try:
        all_hour_users = await db.get_active_users_for_hour(utc_hour)
    except Exception as exc:
        logger.error("Failed to fetch users for hour %d: %s", utc_hour, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch users")

    users = [u for u in all_hour_users if u.delivery_minute_utc == utc_minute]

    if not users:
        logger.info("No users scheduled for UTC %02d:%02d", utc_hour, utc_minute)
        return {"users_processed": 0, "errors": 0}

    unique_topics: set[str] = set()
    for u in users:
        unique_topics.update(u.topics or [])
    logger.info(
        "Run starting: %d users, %d unique topics (UTC %02d:%02d)",
        len(users),
        len(unique_topics),
        utc_hour,
        utc_minute,
    )

    sem = asyncio.Semaphore(settings.USER_PROCESS_CONCURRENCY)

    async def _bounded(user: User, prefetched: dict[str, TopicResult]) -> None:
        async with sem:
            await process_user(user, prefetched)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        follow_redirects=True,
    ) as client:
        prefetched = await prefetch_topic_news(unique_topics, client)
        results = await asyncio.gather(
            *[_bounded(u, prefetched) for u in users], return_exceptions=True
        )

    error_count = sum(1 for r in results if isinstance(r, BaseException))
    logger.info(
        "Run complete: %d users, %d errors (UTC hour %d)",
        len(users),
        error_count,
        utc_hour,
    )
    if error_count > 0:
        raise HTTPException(
            status_code=500,
            detail=f"run_digests: {error_count}/{len(users)} users failed (UTC hour {utc_hour})",
        )
    return {"users_processed": len(users), "errors": 0}


@app.get("/health")
async def health() -> dict:
    """Liveness probe for Cloud Run."""
    return {"status": "ok", "service": "worker"}
