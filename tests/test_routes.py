"""Unit tests for FastAPI route orchestration without live services."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.conftest import make_user
from worker.pipeline.prefetch import TopicResult


class FakeRequest:
    def __init__(self, body: bytes = b"{}", headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


def cached_article(link: str):
    return {
        "title": "Title",
        "link": link,
        "source_name": "Source",
        "snippet": "Snippet",
        "image_url": "",
        "pub_date": "2026-05-04",
        "citation_status": "valid",
    }


class TestApiRoutes:
    def test_health(self) -> None:
        from api import main

        assert asyncio.run(main.health()) == {"status": "ok", "service": "api"}

    def test_webhook_rejects_bad_secret_and_large_bodies(self) -> None:
        from api import main

        with pytest.raises(HTTPException) as bad_secret:
            asyncio.run(main.telegram_webhook(FakeRequest(), x_telegram_bot_api_secret_token="bad"))
        assert bad_secret.value.status_code == 403

        request = FakeRequest(headers={"content-length": str(70_000)})
        with pytest.raises(HTTPException) as too_large:
            asyncio.run(main.telegram_webhook(request, x_telegram_bot_api_secret_token="secret"))
        assert too_large.value.status_code == 413

        body_too_large = FakeRequest(body=b"x" * 70_000)
        with pytest.raises(HTTPException) as body_error:
            asyncio.run(main.telegram_webhook(body_too_large, x_telegram_bot_api_secret_token="secret"))
        assert body_error.value.status_code == 413

    def test_webhook_processes_update_and_returns_processing_error(self, monkeypatch) -> None:
        from api import main

        process_update = AsyncMock()
        main.app.state.ptb_app = SimpleNamespace(bot=object(), process_update=process_update)
        monkeypatch.setattr(main.Update, "de_json", lambda payload, bot: {"update": payload})

        ok = asyncio.run(
            main.telegram_webhook(
                FakeRequest(body=json.dumps({"message": {"text": "/start"}}).encode()),
                x_telegram_bot_api_secret_token="secret",
            )
        )
        assert ok == {"ok": True}
        process_update.assert_awaited_once()

        process_update.side_effect = RuntimeError("bad update")
        error = asyncio.run(
            main.telegram_webhook(
                FakeRequest(body=b"{}"),
                x_telegram_bot_api_secret_token="secret",
            )
        )
        assert error == {"ok": False, "error": "processing error"}


class TestWorkerRoutes:
    def test_health(self) -> None:
        from worker import main

        assert asyncio.run(main.health()) == {"status": "ok", "service": "worker"}

    def test_run_digests_returns_zero_when_no_users(self, monkeypatch) -> None:
        from worker import main

        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[]))

        assert asyncio.run(main.run_digests(object())) == {"users_processed": 0, "errors": 0}

    def test_run_digests_prefetches_unique_topics_and_processes_users(self, monkeypatch) -> None:
        from worker import main

        users = [
            make_user(telegram_id="1", topics=["Tech", "Finance"]),
            make_user(telegram_id="2", topics=["Tech"]),
        ]
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=users))

        async def fake_prefetch(topics, client):
            seen_topics.update(topics)
            return {"Tech": TopicResult("Tech", [])}

        async def fake_process_user(user, prefetched):
            processed.append((user.telegram_id, prefetched))

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        seen_topics: set[str] = set()
        processed: list[tuple[str, dict]] = []
        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(main, "prefetch_topic_news", fake_prefetch)
        monkeypatch.setattr(main, "process_user", fake_process_user)

        result = asyncio.run(main.run_digests(object()))

        assert result == {"users_processed": 2, "errors": 0}
        assert seen_topics == {"Tech", "Finance"}
        assert [p[0] for p in processed] == ["1", "2"]

    def test_prepare_digests_builds_cached_topic_summaries_and_pending_docs(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0})
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(
            main,
            "prefetch_topic_news",
            AsyncMock(return_value={"Tech": TopicResult("Tech", [cached_article("https://example.com/a")])}),
        )
        monkeypatch.setattr(main.llm_cache, "get_cached_curated_topic", AsyncMock(return_value=None))
        monkeypatch.setattr(main.llm_cache, "set_cached_curated_topic", AsyncMock())
        monkeypatch.setattr(
            main,
            "run_curator_for_topic",
            AsyncMock(return_value=[{"article_id": "a1", "topic": "Tech", "url": "https://example.com/a", "relevance_score": 1.0}]),
        )
        monkeypatch.setattr(main.llm_cache, "get_cached_article_summary", AsyncMock(return_value=None))
        monkeypatch.setattr(main.llm_cache, "set_cached_article_summary", AsyncMock())
        monkeypatch.setattr(
            main,
            "run_summariser_for_article",
            AsyncMock(return_value={"article_id": "a1", "summary_points": ["point"], "why_it_matters": "why"}),
        )
        save_pending = AsyncMock()
        monkeypatch.setattr(main.db, "save_pending_digest", save_pending)

        result = asyncio.run(main.prepare_digests(object()))

        assert result["prepared"] == 1
        assert result["topics"] == 1
        assert result["unique_articles_summarised"] == 1
        save_pending.assert_awaited_once()
        saved_articles = save_pending.await_args.args[3]
        assert saved_articles[0]["summary_points"] == ["point"]

    def test_prepare_digests_uses_cache_hits(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0})
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(main, "prefetch_topic_news", AsyncMock(return_value={}))
        monkeypatch.setattr(
            main.llm_cache,
            "get_cached_curated_topic",
            AsyncMock(return_value=[{"article_id": "a1", "topic": "Tech", "url": "https://example.com/a", "relevance_score": 1.0}]),
        )
        monkeypatch.setattr(
            main.llm_cache,
            "get_cached_article_summary",
            AsyncMock(return_value={"article_id": "a1", "summary_points": ["cached"]}),
        )
        save_pending = AsyncMock()
        monkeypatch.setattr(main.db, "save_pending_digest", save_pending)

        result = asyncio.run(main.prepare_digests(object()))

        assert result["prepared"] == 1
        assert save_pending.await_args.args[3][0]["summary_points"] == ["cached"]

    def test_deliver_digests_sends_pending_digest_and_marks_delivered(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", total_digests_sent=2)
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value={"articles": [{"article_id": "a1"}]}))
        monkeypatch.setattr(main, "send_digest_message", AsyncMock(return_value={"status": "delivered", "messages_sent": 1}))
        monkeypatch.setattr(main.db, "save_user_digest_history", AsyncMock())
        update_user = AsyncMock()
        mark_delivered = AsyncMock()
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", mark_delivered)

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {"delivered": 1, "errors": 0}
        update_user.assert_awaited_once()
        assert update_user.await_args.kwargs["total_digests_sent"] == 3
        mark_delivered.assert_awaited_once()

    def test_deliver_digests_falls_back_to_live_pipeline_when_pending_missing(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1")
        process_user = AsyncMock()
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))
        monkeypatch.setattr(main, "process_user", process_user)

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {"delivered": 1, "errors": 0}
        process_user.assert_awaited_once_with(user, prefetched={})

    def test_prepare_digests_skips_save_for_empty_digest(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0})
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(main, "prefetch_topic_news", AsyncMock(return_value={}))
        # Curator returns empty — no articles for this user
        monkeypatch.setattr(main.llm_cache, "get_cached_curated_topic", AsyncMock(return_value=[]))
        save_pending = AsyncMock()
        monkeypatch.setattr(main.db, "save_pending_digest", save_pending)

        result = asyncio.run(main.prepare_digests(object()))

        assert result["prepared"] == 1
        save_pending.assert_not_awaited()

    def test_deliver_digests_skips_state_update_when_zero_messages_sent(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", total_digests_sent=5)
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value={"articles": []}))
        monkeypatch.setattr(
            main,
            "send_digest_message",
            AsyncMock(return_value={"status": "delivered", "messages_sent": 0}),
        )
        update_user = AsyncMock()
        mark_delivered = AsyncMock()
        save_history = AsyncMock()
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", mark_delivered)
        monkeypatch.setattr(main.db, "save_user_digest_history", save_history)

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {"delivered": 1, "errors": 0}
        update_user.assert_not_awaited()
        mark_delivered.assert_not_awaited()
        save_history.assert_not_awaited()

    def test_deliver_digests_skips_state_update_when_pending_articles_empty(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", total_digests_sent=3)
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        # Pending digest exists but articles list is empty (e.g., /prepare saved before guard was added)
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value={"articles": []}))
        monkeypatch.setattr(
            main,
            "send_digest_message",
            AsyncMock(return_value={"status": "delivered", "messages_sent": 0}),
        )
        update_user = AsyncMock()
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", AsyncMock())
        monkeypatch.setattr(main.db, "save_user_digest_history", AsyncMock())

        asyncio.run(main.deliver_digests(object()))

        update_user.assert_not_awaited()
        assert user.total_digests_sent == 3
