"""Per-run topic prefetch: cache-aside over Firestore, parallel citation checks."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from shared.config import settings
from utils.guardrails import validate_topic_policy
from utils.logging import get_logger
from worker.pipeline.news_cache import (
    CachedArticle,
    cache_key,
    get_cached_topic,
    set_cached_topic,
)

logger = get_logger(__name__)

_NEWSDATA_URL = "https://newsdata.io/api/1/latest"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

_BLOCKED_SOURCE_DOMAINS: frozenset[str] = frozenset({
    "baseballnewssource.com",
    "tickerreport.com",
    "watchlistnews.com",
    "zolmax.com",
    "dailypolitical.com",
    "thelincolnianonline.com",
    "williamsonherald.com",
})


@dataclass(frozen=True)
class TopicResult:
    """Pre-UUID NewsData rows for a single topic, ready for per-user UUID minting."""

    topic: str
    raw_articles: list[CachedArticle]


def _is_blocked_source(url: str) -> bool:
    return any(domain in url for domain in _BLOCKED_SOURCE_DOMAINS)


def _is_http_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _is_private_host(hostname: str) -> bool:
    """Return True if the hostname resolves to a private/loopback/link-local address.

    Returns False for unresolvable hostnames — the subsequent HTTP request will
    fail on its own; we only want to block hosts that resolve to internal infrastructure.
    """
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except socket.gaierror:
        return False  # DNS failure — not a private IP, let the HTTP call fail naturally
    except ValueError:
        return True  # Malformed IP literal — block it


def _is_ssrf_safe(url: str) -> bool:
    """Return False for URLs targeting internal/metadata infrastructure."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Explicitly block GCP metadata endpoint by name before DNS
        if hostname in ("metadata.google.internal", "metadata.google.com"):
            return False
        return not _is_private_host(hostname)
    except Exception:
        return False


async def _classify_citation_url(client: httpx.AsyncClient, url: object) -> str:
    """Classify URL reachability without treating bot-blocking as broken."""
    if not _is_http_url(url):
        return "broken"
    if not _is_ssrf_safe(str(url)):
        logger.warning("_classify_citation_url: blocked SSRF attempt for %s", url)
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


async def _fetch_newsdata(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Call NewsData.io with retries on 429/5xx/timeouts. Returns raw `results` list."""
    params = {
        "q": query,
        "language": "en",
        "size": 10,
        "removeduplicate": 1,
        "apikey": settings.NEWSDATA_API_KEY,
    }
    log_params = {**params}
    log_params.pop("apikey", None)

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(settings.RETRY_MAX_ATTEMPTS),
        wait=wait_random_exponential(
            multiplier=settings.RETRY_BACKOFF_BASE_SECONDS,
            max=settings.RETRY_BACKOFF_MAX_SECONDS,
        ),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.ConnectError, _RetriableHTTPError)
        ),
        reraise=True,
    ):
        with attempt:
            logger.info("prefetch: NewsData GET q=%r params=%r", query, log_params)
            resp = await client.get(_NEWSDATA_URL, params=params, timeout=10.0)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                raise _RetriableHTTPError(
                    f"NewsData status {resp.status_code} for q={query!r}"
                )
            resp.raise_for_status()
            data = resp.json()
            return list(data.get("results") or [])

    return []


class _RetriableHTTPError(Exception):
    """Marker for HTTP errors we want Tenacity to retry."""


async def _fetch_one_topic(
    client: httpx.AsyncClient,
    topic: str,
    citation_sem: asyncio.Semaphore,
) -> TopicResult:
    """Fetch (and fall back if needed) raw NewsData rows for one topic, parallel HEAD checks."""
    primary = f"{topic} latest news"
    raw = await _fetch_newsdata(client, primary)

    accepted = await _accept_articles(client, raw, citation_sem)

    if len(accepted) < 3:
        fallback = f"{topic} trending news"
        logger.info(
            "prefetch: topic=%r only %d articles, fallback q=%r",
            topic,
            len(accepted),
            fallback,
        )
        existing_links = {a["link"] for a in accepted}
        fallback_raw = await _fetch_newsdata(client, fallback)
        fallback_accepted = await _accept_articles(client, fallback_raw, citation_sem)
        for a in fallback_accepted:
            if a["link"] not in existing_links:
                accepted.append(a)
                existing_links.add(a["link"])

    logger.info("prefetch: topic=%r accepted %d articles", topic, len(accepted))
    return TopicResult(topic=topic, raw_articles=accepted)


async def _accept_articles(
    client: httpx.AsyncClient,
    results: list[dict],
    citation_sem: asyncio.Semaphore,
) -> list[CachedArticle]:
    """Filter NewsData rows + classify citations in parallel under a concurrency cap."""
    candidates: list[dict] = []
    for a in results:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        link = a.get("link") or ""
        if not link or _is_blocked_source(link):
            continue
        candidates.append(a)

    async def _classify(idx: int, link: str) -> tuple[int, str]:
        async with citation_sem:
            status = await _classify_citation_url(client, link)
        return idx, status

    classifications = await asyncio.gather(
        *[_classify(i, c["link"]) for i, c in enumerate(candidates)]
    )

    accepted: list[CachedArticle] = []
    for i, status in classifications:
        if status == "broken":
            continue
        a = candidates[i]
        accepted.append(
            CachedArticle(
                title=(a.get("title") or "").strip(),
                link=a.get("link") or "",
                source_name=(a.get("source_name") or a.get("source_id") or "").strip(),
                snippet=(a.get("description") or a.get("content") or "").strip(),
                image_url=a.get("image_url") or "",
                pub_date=(a.get("pubDate") or "").strip(),
                citation_status=status,
            )
        )
    return accepted


async def prefetch_topic_news(
    topics: set[str],
    client: httpx.AsyncClient,
) -> dict[str, TopicResult]:
    """Resolve every topic to a TopicResult, using Firestore cache where possible.

    Bounded by NEWS_FETCH_CONCURRENCY for live NewsData fetches.
    Citation HEAD checks within each fetch are bounded by CITATION_CHECK_CONCURRENCY.
    """
    fetch_sem = asyncio.Semaphore(settings.NEWS_FETCH_CONCURRENCY)
    citation_sem = asyncio.Semaphore(settings.CITATION_CHECK_CONCURRENCY)

    async def _resolve(topic: str) -> tuple[str, TopicResult] | None:
        try:
            validate_topic_policy(topic)
        except ValueError:
            logger.warning("prefetch: skipping topic that failed policy check: %r", topic)
            return None

        key = cache_key(topic)
        cached = await get_cached_topic(key)
        if cached is not None:
            logger.info("prefetch: cache HIT topic=%r (%d articles)", topic, len(cached["raw_articles"]))
            return topic, TopicResult(topic=topic, raw_articles=cached["raw_articles"])

        async with fetch_sem:
            result = await _fetch_one_topic(client, topic, citation_sem)

        await set_cached_topic(key, topic, result.raw_articles)
        return topic, result

    resolved = await asyncio.gather(*[_resolve(t) for t in topics], return_exceptions=True)

    out: dict[str, TopicResult] = {}
    for r in resolved:
        if isinstance(r, BaseException):
            logger.error("prefetch: topic resolution failed: %s", r)
            continue
        if r is None:
            continue
        topic, result = r
        out[topic] = result
    return out
