"""Firestore-backed LLM output caches — shared across all users.

Two collections:
  curated_topics_v1/{topic_hash}    — 1h TTL  (curator output per unique topic)
  article_summaries_v1/{url_hash}   — 24h TTL (summariser output per unique URL)

Both follow the same cache-aside pattern as news_cache.py.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from shared.config import settings
from shared.database import _db
from utils.logging import get_logger

logger = get_logger(__name__)

_CURATED_TOPICS_COL = "curated_topics_v1"
_ARTICLE_SUMMARIES_COL = "article_summaries_v1"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _curated_topic_key(topic: str) -> str:
    raw = f"{settings.CURATOR_VERSION}|{topic.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _article_summary_key(url: str) -> str:
    raw = f"{settings.SUMMARISER_VERSION}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _as_aware_utc(ts: object) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    seconds = getattr(ts, "seconds", None)
    if seconds is not None:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    raise TypeError(f"Cannot interpret {ts!r} as a datetime")


def _is_expired(data: dict) -> bool:
    expires_at = data.get("expires_at")
    if expires_at is None:
        return True
    try:
        return _as_aware_utc(expires_at) <= datetime.now(timezone.utc)
    except TypeError:
        return True


# ---------------------------------------------------------------------------
# Curated topics cache
# ---------------------------------------------------------------------------


async def get_cached_curated_topic(topic: str) -> list[dict] | None:
    """Return cached curator output for topic, or None if missing/expired."""
    key = _curated_topic_key(topic)
    try:
        doc = await _db.collection(_CURATED_TOPICS_COL).document(key).get()
    except Exception as exc:
        logger.warning("llm_cache get_cached_curated_topic(%r) failed: %s", topic, exc)
        return None

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    if _is_expired(data):
        return None

    logger.info("llm_cache: curated topic HIT topic=%r articles=%d", topic, len(data.get("curated_articles") or []))
    return list(data.get("curated_articles") or [])


async def set_cached_curated_topic(topic: str, curated_articles: list[dict]) -> None:
    """Persist curator output for topic with TTL = CURATED_TOPIC_TTL_SECONDS."""
    key = _curated_topic_key(topic)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.CURATED_TOPIC_TTL_SECONDS)
    payload = {
        "topic": topic,
        "curated_articles": curated_articles,
        "curated_at": now,
        "expires_at": expires_at,
    }
    try:
        await _db.collection(_CURATED_TOPICS_COL).document(key).set(payload)
        logger.info("llm_cache: curated topic SET topic=%r articles=%d", topic, len(curated_articles))
    except Exception as exc:
        logger.warning("llm_cache set_cached_curated_topic(%r) failed: %s", topic, exc)


# ---------------------------------------------------------------------------
# Article summaries cache
# ---------------------------------------------------------------------------


async def get_cached_article_summary(url: str) -> dict | None:
    """Return cached summariser output for url, or None if missing/expired."""
    key = _article_summary_key(url)
    try:
        doc = await _db.collection(_ARTICLE_SUMMARIES_COL).document(key).get()
    except Exception as exc:
        logger.warning("llm_cache get_cached_article_summary(%r) failed: %s", url, exc)
        return None

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    if _is_expired(data):
        return None

    logger.info("llm_cache: article summary HIT url=%r", url)
    return {k: v for k, v in data.items() if k not in ("summarised_at", "expires_at")}


async def set_cached_article_summary(url: str, summary: dict) -> None:
    """Persist summariser output for url with TTL = ARTICLE_SUMMARY_TTL_SECONDS."""
    key = _article_summary_key(url)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.ARTICLE_SUMMARY_TTL_SECONDS)
    payload = {
        **summary,
        "url": url,
        "summarised_at": now,
        "expires_at": expires_at,
    }
    try:
        await _db.collection(_ARTICLE_SUMMARIES_COL).document(key).set(payload)
        logger.info("llm_cache: article summary SET url=%r", url)
    except Exception as exc:
        logger.warning("llm_cache set_cached_article_summary(%r) failed: %s", url, exc)
