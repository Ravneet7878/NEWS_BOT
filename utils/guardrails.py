"""Input validation and content-policy enforcement for topics, broadcasts, and LLM output."""

import re

from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prompt-injection patterns (used for topic input)
# ---------------------------------------------------------------------------

_INJECTION_RE = re.compile(
    r"```"
    r"|https?://"
    r"|www\."
    r"|ignore\s+instructions"
    r"|system\s+prompt"
    r"|developer\s+message"
    r"|return\s+only"
    r"|you\s+are\s+now"
    r"|\bdisregard\b"
    r"|forget\s+everything"
    r"|\bact\s+as\b"
    r"|pretend\s+(you\s+are|to\s+be)"
    r"|\bjailbreak\b"
    r"|override\s+your"
    r"|ignore\s+all\s+previous"
    r"|new\s+persona"
    r"|roleplay\s+as",
    re.IGNORECASE,
)

# Characters that have no place in a news topic name
_STRUCTURAL_CHARS_RE = re.compile(r'[<>{}[\]\\]')

# ---------------------------------------------------------------------------
# Unsafe-content keywords (topics, broadcasts, and article pre-filter)
# ---------------------------------------------------------------------------

_UNSAFE_KEYWORDS: frozenset[str] = frozenset({
    # Adult / pornographic
    "pornography", " porn ", "xxx", " nude ", "nudity", "sex tape",
    "onlyfans", "prostitut",
    # Hate slurs
    "nigger", "nigga", "faggot", "kike", "wetback",
    # Graphic violence
    "snuff film", "torture porn",
    # Terrorism / extremism (instruction-seeking)
    "bomb making", "how to make a bomb",
    "isis recruitment", "al-qaeda recruit", "terrorist manifesto",
    # Doxxing / harassment
    "doxxing", "swatting",
    # Credential-seeking
    "social security number", "credit card number",
    # Self-harm instructions
    "suicide method", "how to kill myself",
    # Scams
    "advance fee fraud", "nigerian prince",
})

# Regex to strip all HTML tags except <b> and </b>
_DISALLOWED_HTML_RE = re.compile(r"<(?!/?b\b)[^>]+>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_unsafe_content(text: str) -> bool:
    """Return True if text contains adult, violent, or otherwise unsafe keywords."""
    lowered = text.lower()
    return any(kw in lowered for kw in _UNSAFE_KEYWORDS)


def validate_topic_policy(topic: str) -> None:
    """Raise ValueError if topic contains structural chars, injection patterns, or unsafe content."""
    if _STRUCTURAL_CHARS_RE.search(topic):
        raise ValueError(
            f"Topics cannot contain characters like <, >, {{, }}, [, ]. Got: {topic!r}"
        )
    if _INJECTION_RE.search(topic):
        raise ValueError(
            f"That topic looks like a command or URL — please use plain news subjects like "
            f"'Technology' or 'Finance'."
        )
    if is_unsafe_content(topic):
        raise ValueError(
            "One of your topics contains content that isn't allowed. Please choose a different topic."
        )


def sanitize_topics(raw_topics: list[str]) -> list[str]:
    """
    Normalise, deduplicate, and policy-check a list of pre-split topic strings.

    Raises ValueError with a user-friendly message on any violation.
    Returns the cleaned list ready for storage.
    """
    from shared.config import get_settings

    cfg = get_settings()

    normalized: list[str] = []
    seen_lower: set[str] = set()
    for t in raw_topics:
        t = " ".join(t.split())  # trim + collapse internal whitespace
        if not t:
            continue
        key = t.lower()
        if key in seen_lower:
            continue  # case-insensitive dedup
        seen_lower.add(key)
        normalized.append(t)

    if not 1 <= len(normalized) <= cfg.MAX_TOPICS:
        raise ValueError(
            f"Please provide between 1 and {cfg.MAX_TOPICS} topics, separated by commas."
        )

    for t in normalized:
        if len(t) > cfg.MAX_TOPIC_LENGTH:
            raise ValueError(
                f"Topic '{t[:20]}…' is too long. Keep each topic under {cfg.MAX_TOPIC_LENGTH} characters."
            )
        validate_topic_policy(t)

    return normalized


def validate_curated_articles(
    raw_curated: list[dict],
    valid_ids: set[str],
    user_topics: list[str],
) -> list[dict]:
    """
    Drop curator output articles that fail source-of-truth checks.

    valid_ids must be the set of article IDs produced by the Python-controlled fetcher.
    """
    required = ("article_id", "title", "topic", "source")
    user_topic_set = set(user_topics)
    result = []
    for a in raw_curated:
        if not isinstance(a, dict):
            continue
        missing = [f for f in required if not a.get(f)]
        if missing:
            logger.warning("validate_curated_articles: dropping article — missing fields %r", missing)
            continue
        if a["article_id"] not in valid_ids:
            logger.warning("validate_curated_articles: dropping article_id=%r — not in valid_ids", a["article_id"])
            continue
        if a["topic"] not in user_topic_set:
            logger.warning("validate_curated_articles: dropping article_id=%r — topic %r not in user topics", a["article_id"], a["topic"])
            continue
        result.append(a)
    logger.info("validate_curated_articles: %d/%d articles passed", len(result), len(raw_curated))
    return result


def _strip_unsafe_html(text: str) -> str:
    """Remove all HTML tags except <b> and </b>."""
    return _DISALLOWED_HTML_RE.sub("", text)


def validate_final_digest(
    raw_digest: list[dict],
    valid_ids: set[str],
    user_topics: list[str],
    raw_articles_by_id: dict[str, dict],
) -> list[dict]:
    """
    Validate summariser output and overwrite source-of-truth fields from raw fetcher data.

    - article_id must be in valid_ids (Python-fetched)
    - title, topic, source, url, published_at are replaced from raw_articles_by_id
    - summary_points: list of strings, each ≤200 chars, unsafe HTML stripped, capped at 10
    - why_it_matters: ≤500 chars, unsafe HTML stripped
    - Articles with unsafe content in title+summary are dropped
    """
    user_topic_set = set(user_topics)
    # Reverse index for title-based article_id recovery when LLM mutates UUIDs
    title_to_raw: dict[str, dict] = {
        v["title"].strip().lower(): v
        for v in raw_articles_by_id.values()
        if v.get("title")
    }
    result = []
    for a in raw_digest:
        if not isinstance(a, dict):
            continue
        article_id = a.get("article_id", "")
        if not article_id or article_id not in valid_ids:
            raw_by_title = title_to_raw.get((a.get("title") or "").strip().lower())
            if raw_by_title and raw_by_title.get("article_id") in valid_ids:
                logger.warning(
                    "validate_final_digest: LLM changed article_id %r → recovering via title %r",
                    article_id,
                    a.get("title"),
                )
                article_id = raw_by_title["article_id"]
                a["article_id"] = article_id
            else:
                logger.warning(
                    "validate_final_digest: dropping article_id=%r title=%r — not in valid_ids and no title match",
                    article_id,
                    a.get("title"),
                )
                continue

        # Overwrite source-of-truth fields from Python-controlled raw articles
        raw = raw_articles_by_id.get(article_id, {})
        for field in ("title", "topic", "source", "url", "published_at"):
            if raw.get(field):
                a[field] = raw[field]

        if a.get("topic") not in user_topic_set:
            logger.warning(
                "validate_final_digest: dropping article_id=%r — topic %r not in user topics",
                article_id,
                a.get("topic"),
            )
            continue

        # Validate and clean summary_points
        points = a.get("summary_points")
        if not isinstance(points, list) or not points:
            logger.warning(
                "validate_final_digest: dropping article_id=%r — summary_points is %r (expected non-empty list)",
                article_id,
                type(points).__name__,
            )
            continue
        clean_points: list[str] = []
        for p in points:
            if not isinstance(p, str):
                continue
            p = _strip_unsafe_html(p)
            if len(p) > 200:
                p = p[:200]
            if p:
                clean_points.append(p)
        if not clean_points:
            logger.warning("validate_final_digest: dropping article_id=%r — all summary_points empty after cleaning", article_id)
            continue
        a["summary_points"] = clean_points[:10]

        # Clean why_it_matters
        why = a.get("why_it_matters", "")
        if isinstance(why, str):
            why = _strip_unsafe_html(why)
            a["why_it_matters"] = why[:500] if len(why) > 500 else why

        # Safety check on combined text
        title = a.get("title", "")
        summary_text = " ".join(a["summary_points"])
        if is_unsafe_content(f"{title} {summary_text}"):
            logger.warning("validate_final_digest: dropping article_id=%r — unsafe content detected", article_id)
            continue

        result.append(a)
    logger.info("validate_final_digest: %d/%d articles passed", len(result), len(raw_digest))
    return result
