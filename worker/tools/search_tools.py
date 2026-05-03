"""Per-user news assembly: reads prefetched topic data and mints stable SHA-256 article IDs."""

import hashlib
import json

from google.adk.tools import ToolContext  # type: ignore[import-untyped]

from utils.logging import get_logger
from worker.pipeline.prefetch import TopicResult

logger = get_logger(__name__)


def assemble_articles_for_topic(
    topic: str,
    prefetched: TopicResult,
    state: dict,
) -> list[dict]:
    """Build per-user articles from a prefetched TopicResult and update state maps.

    Uses sha256(url)[:16] as article_id so the same URL always maps to the same ID —
    this makes LLM output cacheable across users in the /prepare pipeline.

    Mutates `state["url_map"]`, `state["image_map"]`, `state["citation_status_map"]`,
    `state["published_at_map"]`. Returns the LLM-facing article list (no raw URLs).
    """
    url_map: dict = dict(state.get("url_map") or {})
    image_map: dict = dict(state.get("image_map") or {})
    citation_status_map: dict = dict(state.get("citation_status_map") or {})
    published_at_map: dict = dict(state.get("published_at_map") or {})

    result_articles: list[dict] = []
    for a in prefetched.raw_articles:
        article_id = hashlib.sha256(a["link"].encode()).hexdigest()[:16]
        url_map[article_id] = a["link"]
        citation_status_map[article_id] = a["citation_status"]
        published_at_map[article_id] = a["pub_date"]
        if a["image_url"]:
            image_map[article_id] = a["image_url"]
        result_articles.append({
            "article_id": article_id,
            "title": a["title"],
            "source": a["source_name"],
            "snippet": a["snippet"],
            "published_at": a["pub_date"],
        })

    state["url_map"] = url_map
    state["image_map"] = image_map
    state["citation_status_map"] = citation_status_map
    state["published_at_map"] = published_at_map

    logger.info(
        "assemble_articles_for_topic(%r): %d articles, session maps url=%d image=%d citation=%d dates=%d",
        topic,
        len(result_articles),
        len(url_map),
        len(image_map),
        len(citation_status_map),
        len(published_at_map),
    )
    return result_articles


async def search_news(query: str, tool_context: ToolContext) -> str:
    """Compatibility shim — kept for any caller that hasn't migrated to the prefetch path.

    The hot path no longer goes through this function. Returns an empty list.
    Real fetch logic now lives in worker/pipeline/prefetch.py.
    """
    logger.warning(
        "search_news called for query=%r — this code path is deprecated; expected the prefetch path.",
        query,
    )
    return json.dumps([])
