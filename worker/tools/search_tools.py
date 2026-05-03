"""Custom news search tool — fetches real URLs and images via NewsData.io API."""

import json
import uuid
from typing import Literal

import httpx
from google.adk.tools import ToolContext  # type: ignore[import-untyped]

from shared.config import settings
from utils.logging import get_logger

logger = get_logger(__name__)

_NEWSDATA_URL = "https://newsdata.io/api/1/latest"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
CitationStatus = Literal["valid", "broken", "blocked"]

_BLOCKED_SOURCE_DOMAINS: frozenset[str] = frozenset({
    "baseballnewssource.com",
    "tickerreport.com",
    "watchlistnews.com",
    "zolmax.com",
    "dailypolitical.com",
    "thelincolnianonline.com",
    "williamsonherald.com",
})


def _is_blocked_source(url: str) -> bool:
    return any(domain in url for domain in _BLOCKED_SOURCE_DOMAINS)


def _is_http_url(url: object) -> bool:
    """Return True for syntactically usable HTTP(S) article URLs."""
    return isinstance(url, str) and url.startswith(("http://", "https://"))


async def _classify_citation_url(
    client: httpx.AsyncClient,
    url: object,
) -> CitationStatus:
    """Classify URL reachability without treating bot-blocking as broken."""
    if not _is_http_url(url):
        return "broken"
    try:
        resp = await client.head(str(url), timeout=5.0)
        if resp.status_code == 405:
            resp = await client.get(str(url), timeout=5.0)
    except Exception:
        return "blocked"
    if resp.status_code in (404, 410):
        return "broken"
    if 200 <= resp.status_code < 400:
        return "valid"
    return "blocked"


async def search_news(query: str, tool_context: ToolContext) -> str:
    """
    Search NewsData.io Latest API for the given query.

    Stores {article_id: url} in session state["url_map"].
    Stores {article_id: image_url} in session state["image_map"].
    Stores {article_id: citation_status} in session state["citation_status_map"].
    Stores {article_id: published_at} in session state["published_at_map"].
    Returns [{article_id, title, source, snippet, published_at}] to the LLM — no raw URLs, no images.
    """
    logger.info("search_news: starting query=%r", query)
    log_params = {"q": query, "language": "en", "size": 10, "removeduplicate": 1}
    logger.info("search_news: NewsData.io request params=%r (apikey omitted)", log_params)
    try:
        async with httpx.AsyncClient(headers=_HEADERS) as client:
            resp = await client.get(
                _NEWSDATA_URL,
                params={**log_params, "apikey": settings.NEWSDATA_API_KEY},
                timeout=10.0,
            )
            logger.info(
                "search_news: HTTP %d for query=%r", resp.status_code, query
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("search_news(%r) failed: %s", query, exc)
        return json.dumps([])

    results = data.get("results", [])
    logger.info("NewsData.io API returned %d results for query %r", len(results), query)

    # Store real URLs/images/statuses/dates in session state — LLM never sees these.
    url_map: dict = dict(tool_context.state.get("url_map") or {})
    image_map: dict = dict(tool_context.state.get("image_map") or {})
    citation_status_map: dict = dict(tool_context.state.get("citation_status_map") or {})
    published_at_map: dict = dict(tool_context.state.get("published_at_map") or {})
    result_articles = []
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        for a in results:
            title = (a.get("title") or "").strip()
            if not title:
                logger.warning("search_news(%r): skipping article with missing title", query)
                continue
            url = a.get("link") or ""
            if not url:
                logger.warning(
                    "search_news(%r): skipping article with no URL: title=%r", query, title
                )
                continue
            citation_status = await _classify_citation_url(client, url)
            if citation_status == "broken":
                logger.warning(
                    "search_news(%r): skipping article with broken citation: title=%r url=%r",
                    query,
                    title,
                    url,
                )
                continue
            if _is_blocked_source(url):
                logger.info(
                    "search_news(%r): blocked aggregator source: title=%r url=%r",
                    query,
                    title,
                    url,
                )
                continue
            article_id = str(uuid.uuid4())
            image = a.get("image_url") or ""
            source_name = (a.get("source_name") or a.get("source_id") or "").strip()
            snippet = (a.get("description") or a.get("content") or "").strip()
            pub_date = (a.get("pubDate") or "").strip()
            logger.info(
                "search_news(%r): fetched article | title=%r | source=%r | url=%r"
                " | image=%r | pubDate=%r | citation=%s",
                query,
                title,
                source_name,
                url,
                image or None,
                pub_date,
                citation_status,
            )
            url_map[article_id] = url
            citation_status_map[article_id] = citation_status
            published_at_map[article_id] = pub_date
            if image:
                image_map[article_id] = image
            result_articles.append({
                "article_id": article_id,
                "title": title,
                "source": source_name,
                "snippet": snippet,
                "published_at": pub_date,
            })

    logger.info(
        "search_news(%r): %d/%d articles accepted (after citation filtering)",
        query,
        len(result_articles),
        len(results),
    )
    tool_context.state["url_map"] = url_map
    tool_context.state["image_map"] = image_map
    tool_context.state["citation_status_map"] = citation_status_map
    tool_context.state["published_at_map"] = published_at_map
    logger.info(
        "search_news(%r): session maps — url=%d image=%d citation=%d dates=%d",
        query,
        len(url_map),
        len(image_map),
        len(citation_status_map),
        len(published_at_map),
    )

    # Return to LLM: article_id + title + source + snippet + published_at — no raw URLs, no images
    return json.dumps(result_articles)
