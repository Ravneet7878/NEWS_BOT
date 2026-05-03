"""Deterministic news fetcher — queries built from configured topics only."""

import json

from utils.logging import get_logger
from worker.tools.search_tools import search_news

logger = get_logger(__name__)


class _StateProxy:
    """Minimal shim so search_news can write to a plain dict via tool_context.state."""

    def __init__(self, state: dict) -> None:
        self.state = state


async def fetch_articles_for_user(prefs: dict, state: dict) -> None:
    """
    Deterministically fetch articles for all configured topics.

    Reads:  prefs["topics"] and prefs["topic_weights"]
    Writes: state["raw_articles"] (JSON string)
            state["url_map"], state["image_map"],
            state["citation_status_map"], state["published_at_map"]
    """
    topics: list[str] = prefs.get("topics", [])
    weights: dict[str, float] = prefs.get("topic_weights", {})

    logger.info("Fetcher: configured topics=%r weights=%r", topics, weights)

    if not topics:
        logger.warning("Fetcher: no configured topics — skipping fetch")
        state["raw_articles"] = "[]"
        return

    topic_set = set(topics)
    sorted_topics = sorted(topics, key=lambda t: weights.get(t, 1.0), reverse=True)
    ctx = _StateProxy(state)
    all_articles: list[dict] = []

    for topic in sorted_topics:
        query = f"{topic} latest news"
        logger.info("Fetcher: topic=%r q=%r", topic, query)
        articles: list[dict] = json.loads(await search_news(query, ctx))

        if len(articles) < 3:
            fallback = f"{topic} trending news"
            logger.info(
                "Fetcher: topic=%r only %d articles, fallback q=%r",
                topic,
                len(articles),
                fallback,
            )
            existing_ids = {a["article_id"] for a in articles}
            for a in json.loads(await search_news(fallback, ctx)):
                if a["article_id"] not in existing_ids:
                    articles.append(a)
                    existing_ids.add(a["article_id"])

        for a in articles:
            a["topic"] = topic  # deterministic — never LLM-assigned
            a["url"] = ""
            a["summary"] = a.get("snippet", "")

        logger.info("Fetcher: topic=%r accepted %d articles", topic, len(articles))
        all_articles.extend(articles)

    # Belt-and-suspenders: drop any article whose topic isn't in the configured set
    validated = [a for a in all_articles if a.get("topic") in topic_set]
    dropped = len(all_articles) - len(validated)
    if dropped:
        logger.error("Fetcher: dropped %d articles with unconfigured topics", dropped)

    logger.info("Fetcher: total raw articles=%d", len(validated))
    state["raw_articles"] = json.dumps(validated)
