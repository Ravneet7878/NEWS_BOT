"""Per-user article assembly from prefetched topic data."""

import json

from utils.logging import get_logger
from worker.pipeline.prefetch import TopicResult
from worker.tools.search_tools import assemble_articles_for_topic

logger = get_logger(__name__)


async def fetch_articles_for_user(
    prefs: dict,
    state: dict,
    prefetched: dict[str, TopicResult],
) -> None:
    """Assemble per-user raw_articles from prefetched topic results.

    Reads:  prefs["topics"], prefs["topic_weights"], prefetched
    Writes: state["raw_articles"], state["url_map"], state["image_map"],
            state["citation_status_map"], state["published_at_map"]

    Cross-topic URL dedup: if a URL appears in multiple topics, it's assigned
    to the user's highest-weighted matching topic.
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

    seen_links: set[str] = set()
    all_articles: list[dict] = []

    for topic in sorted_topics:
        result = prefetched.get(topic)
        if result is None:
            logger.warning("Fetcher: no prefetched result for topic=%r", topic)
            continue

        # Dedup by URL across topics — first (highest-weight) topic wins
        unique = TopicResult(
            topic=topic,
            raw_articles=[a for a in result.raw_articles if a["link"] not in seen_links],
        )
        for a in unique.raw_articles:
            seen_links.add(a["link"])

        articles = assemble_articles_for_topic(topic, unique, state)
        for a in articles:
            a["topic"] = topic
            a["url"] = ""
            a["summary"] = a.get("snippet", "")

        logger.info("Fetcher: topic=%r assembled %d articles", topic, len(articles))
        all_articles.extend(articles)

    validated = [a for a in all_articles if a.get("topic") in topic_set]
    dropped = len(all_articles) - len(validated)
    if dropped:
        logger.error("Fetcher: dropped %d articles with unconfigured topics", dropped)

    logger.info("Fetcher: total raw articles=%d", len(validated))
    state["raw_articles"] = json.dumps(validated)
