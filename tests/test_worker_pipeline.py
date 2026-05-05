"""Unit tests for worker-side article assembly, prefetching, and delivery helpers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from worker.pipeline.fetcher import fetch_articles_for_user
from worker.pipeline.prefetch import TopicResult
from worker.tools.search_tools import assemble_articles_for_topic


def article(link: str, title: str = "Title", **overrides):
    data = {
        "title": title,
        "link": link,
        "source_name": "Source",
        "snippet": "Snippet",
        "image_url": "",
        "pub_date": "2026-05-04",
        "citation_status": "valid",
    }
    data.update(overrides)
    return data


class TestArticleAssembly:
    def test_assemble_articles_updates_source_maps_and_hides_url_from_llm(self) -> None:
        state = {}
        prefetched = TopicResult(
            topic="Tech",
            raw_articles=[
                article(
                    "https://example.com/a",
                    title="A",
                    image_url="https://img.example/a.png",
                    citation_status="blocked",
                )
            ],
        )

        [assembled] = assemble_articles_for_topic("Tech", prefetched, state)
        article_id = assembled["article_id"]

        assert assembled["title"] == "A"
        assert "url" not in assembled
        assert state["url_map"][article_id] == "https://example.com/a"
        assert state["image_map"][article_id] == "https://img.example/a.png"
        assert state["citation_status_map"][article_id] == "blocked"
        assert state["published_at_map"][article_id] == "2026-05-04"

    def test_fetch_articles_deduplicates_urls_by_highest_topic_weight(self) -> None:
        state = {}
        prefs = {
            "topics": ["Low", "High"],
            "topic_weights": {"Low": 1.0, "High": 3.0},
        }
        prefetched = {
            "Low": TopicResult("Low", [article("https://example.com/shared", title="Low article")]),
            "High": TopicResult("High", [article("https://example.com/shared", title="High article")]),
        }

        asyncio.run(fetch_articles_for_user(prefs, state, prefetched))
        raw = json.loads(state["raw_articles"])

        assert len(raw) == 1
        assert raw[0]["title"] == "High article"
        assert raw[0]["topic"] == "High"
        assert raw[0]["url"] == ""

    def test_fetch_articles_without_topics_writes_empty_list(self) -> None:
        state = {}

        asyncio.run(fetch_articles_for_user({"topics": []}, state, {}))

        assert state["raw_articles"] == "[]"


class TestPrefetch:
    def test_url_helpers_and_blocked_sources(self) -> None:
        from worker.pipeline import prefetch

        assert prefetch._is_http_url("https://example.com") is True
        assert prefetch._is_http_url("ftp://example.com") is False
        assert prefetch._is_http_url(None) is False
        assert prefetch._is_blocked_source("https://tickerreport.com/story") is True
        assert prefetch._is_blocked_source("https://direct.example/story") is False

    def test_classifies_citations(self) -> None:
        from worker.pipeline import prefetch

        class Client:
            def __init__(self, status: int, get_status: int | None = None, fail: bool = False) -> None:
                self.status = status
                self.get_status = get_status
                self.fail = fail

            async def head(self, url: str, timeout: float):
                if self.fail:
                    raise RuntimeError("blocked")
                return SimpleNamespace(status_code=self.status)

            async def get(self, url: str, timeout: float):
                return SimpleNamespace(status_code=self.get_status)

        assert asyncio.run(prefetch._classify_citation_url(Client(200), "https://ok.example")) == "valid"
        assert asyncio.run(prefetch._classify_citation_url(Client(405, 200), "https://ok.example")) == "valid"
        assert asyncio.run(prefetch._classify_citation_url(Client(404), "https://gone.example")) == "broken"
        assert asyncio.run(prefetch._classify_citation_url(Client(403), "https://blocked.example")) == "blocked"
        assert asyncio.run(prefetch._classify_citation_url(Client(200), "bad-url")) == "broken"
        assert asyncio.run(prefetch._classify_citation_url(Client(200, fail=True), "https://timeout.example")) == "blocked"

    def test_accept_articles_filters_titles_links_blocked_and_broken_citations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.pipeline import prefetch

        async def fake_classify(client, url):
            return "broken" if "broken" in url else "blocked" if "blocked" in url else "valid"

        monkeypatch.setattr(prefetch, "_classify_citation_url", fake_classify)
        rows = [
            {"title": "", "link": "https://example.com/no-title"},
            {"title": "No link", "link": ""},
            {"title": "Blocked source", "link": "https://tickerreport.com/a"},
            {"title": "Broken", "link": "https://example.com/broken"},
            {"title": "Blocked", "link": "https://example.com/blocked", "source_name": "S"},
            {"title": "Valid", "link": "https://example.com/valid", "description": "D"},
        ]

        accepted = asyncio.run(prefetch._accept_articles(object(), rows, asyncio.Semaphore(10)))

        assert [a["title"] for a in accepted] == ["Blocked", "Valid"]
        assert accepted[0]["citation_status"] == "blocked"
        assert accepted[1]["snippet"] == "D"

    def test_fetch_one_topic_uses_trending_fallback_only_when_few_articles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.pipeline import prefetch

        calls: list[str] = []

        async def fake_fetch(client, query):
            calls.append(query)
            if "latest" in query:
                return [{"title": "One", "link": "https://example.com/1"}]
            return [{"title": "Two", "link": "https://example.com/2"}]

        async def fake_accept(client, rows, sem):
            return [
                article(row["link"], row["title"])
                for row in rows
            ]

        monkeypatch.setattr(prefetch, "_fetch_newsdata", fake_fetch)
        monkeypatch.setattr(prefetch, "_accept_articles", fake_accept)

        result = asyncio.run(prefetch._fetch_one_topic(object(), "Tech", asyncio.Semaphore(10)))

        assert calls == ["Tech latest news", "Tech trending news"]
        assert [a["title"] for a in result.raw_articles] == ["One", "Two"]

    def test_prefetch_topic_news_uses_cache_and_skips_invalid_topics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.pipeline import prefetch

        async def fake_get_cached(key):
            if key.endswith("cached"):
                return {"topic": "Cached", "raw_articles": [article("https://example.com/cached")]}
            return None

        async def fake_set_cached(key, topic, raw_articles):
            saved.append((key, topic, raw_articles))

        async def fake_fetch_one(client, topic, sem):
            return TopicResult(topic, [article(f"https://example.com/{topic}")])

        def fake_cache_key(topic):
            return "cached" if topic == "Cached" else f"key-{topic}"

        saved = []
        monkeypatch.setattr(prefetch, "cache_key", fake_cache_key)
        monkeypatch.setattr(prefetch, "get_cached_topic", fake_get_cached)
        monkeypatch.setattr(prefetch, "set_cached_topic", fake_set_cached)
        monkeypatch.setattr(prefetch, "_fetch_one_topic", fake_fetch_one)

        out = asyncio.run(prefetch.prefetch_topic_news({"Cached", "Live", "http://bad.example"}, object()))

        assert set(out) == {"Cached", "Live"}
        assert saved[0][1] == "Live"


class TestWorkerPureHelpers:
    def test_parse_llm_json_handles_fences_embedded_arrays_and_bad_unicode(self) -> None:
        from worker import main

        assert main._parse_llm_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
        assert main._parse_llm_json('prefix [{"a": "\\u bad"}] suffix') == [{"a": "\\u bad"}]
        assert main._parse_llm_json("") == []

    def test_normalize_articles_handles_strings_and_rejects_bad_shapes(self) -> None:
        from worker import main

        raw = [{"a": 1}, '{"b": 2}', "bad", 3]

        assert main._normalize_articles(raw) == [{"a": 1}, {"b": 2}]
        assert main._normalize_articles(json.dumps(raw[:2])) == [{"a": 1}, {"b": 2}]
        assert main._normalize_articles({"a": 1}) == []

    def test_weighted_topic_selection_allocates_by_weight_and_relevance(self) -> None:
        from worker import main

        articles = [
            {"topic": "Low", "title": "low-1", "relevance_score": 0.9},
            {"topic": "High", "title": "high-1", "relevance_score": 0.1},
            {"topic": "High", "title": "high-2", "relevance_score": 0.8},
            {"topic": "High", "title": "high-3", "relevance_score": 0.7},
        ]

        selected = main._weighted_topic_selection(
            articles,
            {"Low": 1.0, "High": 3.0},
            total_slots=3,
        )

        assert [a["title"] for a in selected] == ["high-2", "high-3", "low-1"]

    def test_url_injection_and_deliverable_filtering(self) -> None:
        from worker import main

        articles = [
            {"article_id": "a1", "title": "A"},
            {"article_id": "a2", "title": "B", "url": "https://already.example"},
            {"article_id": "a3", "title": "C"},
        ]

        injected = main._inject_digest_assets(
            articles,
            {"a1": "https://example.com/a1"},
            {"a1": "https://img.example/a1.png"},
            {"a1": "valid", "a2": "broken"},
            {"a1": "2026-05-04"},
            "user",
        )

        assert injected[0]["url"] == "https://example.com/a1"
        assert injected[0]["image_url"] == "https://img.example/a1.png"
        assert injected[0]["published_at"] == "2026-05-04"
        assert main._filter_deliverable_articles(injected) == [injected[0]]

    def test_flatten_and_dedup_prepare_helpers(self) -> None:
        from worker import main

        curated_by_topic = {
            "Tech": [{"url": "https://a", "topic": "Tech"}, {"url": "https://shared", "topic": "Tech"}],
            "Finance": [{"url": "https://shared", "topic": "Finance"}, {"url": "https://b", "topic": "Finance"}],
        }

        flattened = main._flatten_curated_for_user(curated_by_topic, ["Tech", "Finance"])
        deduped = main._dedup_articles_by_url({"u1": flattened, "u2": [{"url": "https://a"}]})

        assert [a["url"] for a in flattened] == ["https://a", "https://shared", "https://b"]
        assert [a["url"] for a in deduped] == ["https://a", "https://shared", "https://b"]


class TestTelegramFormatting:
    def test_escape_with_bold_preserves_only_bold_tags(self) -> None:
        from worker.tools import telegram_tools

        assert telegram_tools._escape_with_bold("<b>Google</b> <i>x</i>") == "<b>Google</b> &lt;i&gt;x&lt;/i&gt;"

    def test_feedback_keyboard_truncates_callback_bytes_and_replaces_colons(self) -> None:
        from worker.tools import telegram_tools

        markup = telegram_tools._build_feedback_keyboard("Very:Long:" + "x" * 80, "abcdef123456")
        callback = markup.inline_keyboard[0][0].callback_data

        assert len(callback.encode("utf-8")) <= 64
        assert callback.startswith("feedback:more:Very-Long-")
        assert callback.endswith(":abcdef12")

    def test_send_digest_message_formats_articles_and_deduplicates_bullets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.tools import telegram_tools

        sent: list[dict] = []

        async def fake_send(**kwargs):
            sent.append(kwargs)

        monkeypatch.setattr(telegram_tools, "_send_with_retry", fake_send)
        digest = json.dumps(
            [
                {
                    "article_id": "abcdef123456",
                    "title": "A < B",
                    "topic": "Tech",
                    "source": "Source",
                    "url": "https://example.com/?q=<x>",
                    "citation_status": "valid",
                    "published_at": "2026-05-04",
                    "summary_points": ["<b>Google</b> grew", "<b>Google</b> grew", "Second point"],
                    "why_it_matters": "<b>Google</b> wins",
                },
                {
                    "title": "Blocked",
                    "topic": "Finance",
                    "source": "Publisher",
                    "url": "https://publisher.example",
                    "citation_status": "blocked",
                    "published_at": "",
                    "summary": "Legacy summary",
                },
            ]
        )

        result = asyncio.run(telegram_tools.send_digest_message("123", digest))

        assert result == {"status": "delivered", "messages_sent": 2}
        assert len(sent) == 2
        assert "A &lt; B" in sent[0]["text"]
        assert sent[0]["text"].count("<b>Google</b> grew") == 1
        assert 'href="https://example.com/?q=&lt;x&gt;"' in sent[0]["text"]
        assert sent[0]["reply_markup"] is not None
        assert "citation unavailable: publisher blocked verification" in sent[1]["text"]
        assert "Date: Unknown" in sent[1]["text"]

    def test_send_digest_message_trims_long_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.tools import telegram_tools

        sent: list[dict] = []

        async def fake_send(**kwargs):
            sent.append(kwargs)

        monkeypatch.setattr(telegram_tools, "_send_with_retry", fake_send)
        digest = json.dumps(
            [
                {
                    "title": "Long",
                    "topic": "Tech",
                    "source": "Source",
                    "url": "https://example.com",
                    "summary_points": ["x" * 1000 for _ in range(8)],
                    "why_it_matters": "y" * 2000,
                }
            ]
        )

        result = asyncio.run(telegram_tools.send_digest_message("123", digest))

        assert result["messages_sent"] == 1
        assert len(sent[0]["text"]) <= 4096

    def test_send_digest_message_returns_error_on_send_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from worker.tools import telegram_tools

        async def fake_send(**kwargs):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(telegram_tools, "_send_with_retry", fake_send)

        result = asyncio.run(telegram_tools.send_digest_message("123", json.dumps([{"summary": "x"}])))

        assert "telegram down" in result["error"]
