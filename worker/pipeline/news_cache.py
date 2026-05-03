"""Firestore-backed 24h cache for NewsData topic queries.

The cache stores the **pre-UUID** NewsData payload — each user mints fresh
article_id UUIDs at fetch time so per-user session state stays isolated.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from shared.config import settings
from shared.database import _db
from utils.logging import get_logger

logger = get_logger(__name__)

_CACHE_COL = "news_cache"


class CachedArticle(TypedDict):
    title: str
    link: str
    source_name: str
    snippet: str
    image_url: str
    pub_date: str
    citation_status: str  # "valid" | "blocked"


class TopicCachePayload(TypedDict):
    topic: str
    raw_articles: list[CachedArticle]


def cache_key(topic: str, language: str = "en", size: int = 10, endpoint: str = "latest") -> str:
    """Stable, deterministic Firestore document ID for a topic query."""
    raw = f"{settings.NEWS_CACHE_KEY_VERSION}|{topic.lower().strip()}|{language}|{size}|{endpoint}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_cached_topic(key: str) -> TopicCachePayload | None:
    """Return the cached payload if present and unexpired, else None.

    Read failures fall through to None — the caller will re-fetch from NewsData.
    """
    try:
        doc = await _db.collection(_CACHE_COL).document(key).get()
    except Exception as exc:
        logger.warning("news_cache get_cached_topic(%s) failed: %s", key, exc)
        return None

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    expires_at = data.get("expires_at")
    now = datetime.now(timezone.utc)
    if expires_at is None or _as_aware_utc(expires_at) <= now:
        return None

    return TopicCachePayload(
        topic=data.get("topic", ""),
        raw_articles=list(data.get("raw_articles") or []),
    )


async def set_cached_topic(key: str, topic: str, raw_articles: list[CachedArticle]) -> None:
    """Persist a topic payload with TTL = NEWS_CACHE_TTL_SECONDS. Fire-and-forget."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.NEWS_CACHE_TTL_SECONDS)
    payload = {
        "topic": topic,
        "fetched_at": now,
        "expires_at": expires_at,
        "raw_articles": raw_articles,
    }
    try:
        await _db.collection(_CACHE_COL).document(key).set(payload)
    except Exception as exc:
        logger.warning("news_cache set_cached_topic(%s) failed: %s", key, exc)


def _as_aware_utc(ts: object) -> datetime:
    """Coerce a Firestore Timestamp / datetime to a tz-aware UTC datetime."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    # Firestore Timestamp objects expose a .seconds/.nanoseconds attribute pair
    seconds = getattr(ts, "seconds", None)
    if seconds is not None:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    raise TypeError(f"Cannot interpret {ts!r} as a datetime")
