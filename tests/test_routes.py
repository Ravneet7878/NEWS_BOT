"""Unit tests for FastAPI route orchestration without live services."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
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

        # Users have delivery_minute_utc=0 (default); pin utc_now to minute=0 so the filter passes.
        users = [
            make_user(telegram_id="1", topics=["Tech", "Finance"]),
            make_user(telegram_id="2", topics=["Tech"]),
        ]
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
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

        # Pin utc_now to 12:00; window = 12:01–12:05. User slot = 12:01 → matches first slot.
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc))
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=12, delivery_minute_utc=1)
        monkeypatch.setattr(main.db, "get_active_users_for_window", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))

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

    def test_prepare_digests_targets_next_utc_day_before_midnight(self, monkeypatch) -> None:
        from worker import main

        # utc_now = 23:35 → window covers 23:36–23:40 UTC. User delivery slot = 23:36 (next day)
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=23, delivery_minute_utc=36)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 23, 35, tzinfo=timezone.utc))
        get_window = AsyncMock(return_value=[user])
        monkeypatch.setattr(main.db, "get_active_users_for_window", get_window)
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))

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
        get_window.assert_awaited_once_with([23])
        save_pending.assert_awaited_once()
        # slot 23:36 on 2026-05-04 (same day since cursor starts at 23:36)
        assert save_pending.await_args.args[1] == date(2026, 5, 4)
        assert save_pending.await_args.args[2] == 23

    def test_prepare_digests_uses_cache_hits(self, monkeypatch) -> None:
        from worker import main

        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc))
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=12, delivery_minute_utc=1)
        monkeypatch.setattr(main.db, "get_active_users_for_window", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))

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
        # get_active_users_for_hour returns the user; delivery_minute_utc==0 matches minute 0
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value={"articles": [{"article_id": "a1"}]}))
        monkeypatch.setattr(main, "send_digest_message", AsyncMock(return_value={"status": "delivered", "messages_sent": 1}))
        monkeypatch.setattr(main.db, "save_user_digest_history", AsyncMock())
        update_user = AsyncMock()
        mark_delivered = AsyncMock()
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", mark_delivered)

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {
            "users_total": 1,
            "delivered": 1,
            "already_delivered": 0,
            "skipped_empty": 0,
            "missing_pending": 0,
            "errors": 0,
        }
        update_user.assert_awaited_once()
        assert update_user.await_args.kwargs["total_digests_sent"] == 3
        mark_delivered.assert_awaited_once()

    def test_deliver_digests_returns_retryable_error_when_pending_missing(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1")
        process_user = AsyncMock()
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))
        monkeypatch.setattr(main, "process_user", process_user)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(main.deliver_digests(object()))

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == {
            "users_total": 1,
            "delivered": 0,
            "already_delivered": 0,
            "skipped_empty": 0,
            "missing_pending": 1,
            "errors": 0,
        }
        process_user.assert_not_awaited()

    def test_deliver_digests_skips_users_whose_minute_does_not_match(self, monkeypatch) -> None:
        from worker import main

        # User delivery slot is :30 but deliver fires at :00 — should be a zero-op
        user = make_user(telegram_id="1", delivery_minute_utc=30)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {
            "users_total": 0,
            "delivered": 0,
            "already_delivered": 0,
            "skipped_empty": 0,
            "missing_pending": 0,
            "errors": 0,
        }

    def test_prepare_digests_skips_save_for_empty_digest(self, monkeypatch) -> None:
        from worker import main

        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc))
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=12, delivery_minute_utc=1)
        monkeypatch.setattr(main.db, "get_active_users_for_window", AsyncMock(return_value=[user]))
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))

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
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
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

        assert result == {
            "users_total": 1,
            "delivered": 0,
            "already_delivered": 0,
            "skipped_empty": 1,
            "missing_pending": 0,
            "errors": 0,
        }
        update_user.assert_not_awaited()
        mark_delivered.assert_not_awaited()
        save_history.assert_not_awaited()

    def test_deliver_digests_skips_state_update_when_pending_articles_empty(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", total_digests_sent=3)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
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

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {
            "users_total": 1,
            "delivered": 0,
            "already_delivered": 0,
            "skipped_empty": 1,
            "missing_pending": 0,
            "errors": 0,
        }
        update_user.assert_not_awaited()
        assert user.total_digests_sent == 3

    def test_deliver_digests_skips_already_delivered_pending_digest(self, monkeypatch) -> None:
        from worker import main

        user = make_user(telegram_id="1", total_digests_sent=3)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=[user]))
        monkeypatch.setattr(
            main.db,
            "get_pending_digest",
            AsyncMock(return_value={"articles": [{"article_id": "a1"}], "delivered_at": "done"}),
        )
        send_digest = AsyncMock()
        update_user = AsyncMock()
        mark_delivered = AsyncMock()
        save_history = AsyncMock()
        monkeypatch.setattr(main, "send_digest_message", send_digest)
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", mark_delivered)
        monkeypatch.setattr(main.db, "save_user_digest_history", save_history)

        result = asyncio.run(main.deliver_digests(object()))

        assert result == {
            "users_total": 1,
            "delivered": 0,
            "already_delivered": 1,
            "skipped_empty": 0,
            "missing_pending": 0,
            "errors": 0,
        }
        send_digest.assert_not_awaited()
        update_user.assert_not_awaited()
        mark_delivered.assert_not_awaited()
        save_history.assert_not_awaited()

    def test_deliver_digests_mixed_results_raise_retryable_for_missing_pending(self, monkeypatch) -> None:
        from worker import main

        users = [
            make_user(telegram_id="delivered", total_digests_sent=1),
            make_user(telegram_id="already", total_digests_sent=2),
            make_user(telegram_id="missing", total_digests_sent=3),
        ]
        pending_by_user = {
            "delivered": {"articles": [{"article_id": "a1"}]},
            "already": {"articles": [{"article_id": "a2"}], "delivered_at": "done"},
            "missing": None,
        }

        async def fake_get_pending(telegram_id, target_date, target_hour, target_minute=0):
            return pending_by_user[telegram_id]

        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(main.db, "get_active_users_for_hour", AsyncMock(return_value=users))
        monkeypatch.setattr(main.db, "get_pending_digest", fake_get_pending)
        send_digest = AsyncMock(return_value={"status": "delivered", "messages_sent": 1})
        update_user = AsyncMock()
        mark_delivered = AsyncMock()
        save_history = AsyncMock()
        monkeypatch.setattr(main, "send_digest_message", send_digest)
        monkeypatch.setattr(main.db, "update_user", update_user)
        monkeypatch.setattr(main.db, "mark_pending_delivered", mark_delivered)
        monkeypatch.setattr(main.db, "save_user_digest_history", save_history)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(main.deliver_digests(object()))

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == {
            "users_total": 3,
            "delivered": 1,
            "already_delivered": 1,
            "skipped_empty": 0,
            "missing_pending": 1,
            "errors": 0,
        }
        send_digest.assert_awaited_once()
        assert send_digest.await_args.args[0] == "delivered"
        update_user.assert_awaited_once()
        assert update_user.await_args.args[0] == "delivered"
        assert update_user.await_args.kwargs["total_digests_sent"] == 2
        mark_delivered.assert_awaited_once()
        save_history.assert_awaited_once()

    def test_prepare_digests_picks_up_user_after_time_change(self, monkeypatch) -> None:
        from worker import main

        # utc_now = 12:32 UTC, PREPARE_BUFFER_MINUTES=5 → window covers 12:33–12:37.
        # User changed delivery slot to 12:35 after the 12:30 prepare already ran.
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=12, delivery_minute_utc=35)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 12, 32, tzinfo=timezone.utc))
        monkeypatch.setattr(main.settings, "PREPARE_BUFFER_MINUTES", 5)
        get_window = AsyncMock(return_value=[user])
        monkeypatch.setattr(main.db, "get_active_users_for_window", get_window)
        # No pending doc exists yet — must be built
        monkeypatch.setattr(main.db, "get_pending_digest", AsyncMock(return_value=None))

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
        save_pending.assert_awaited_once()
        assert save_pending.await_args.args[2] == 12   # target_hour
        assert save_pending.await_args.args[4] == 35   # target_minute

    def test_prepare_digests_skips_user_with_existing_pending_doc(self, monkeypatch) -> None:
        from worker import main

        # Same setup as above, but get_pending_digest returns an existing doc — must NOT rebuild.
        user = make_user(telegram_id="1", topics=["Tech"], topic_weights={"Tech": 1.0}, delivery_hour_utc=12, delivery_minute_utc=35)
        monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 5, 4, 12, 32, tzinfo=timezone.utc))
        monkeypatch.setattr(main.settings, "PREPARE_BUFFER_MINUTES", 5)
        monkeypatch.setattr(main.db, "get_active_users_for_window", AsyncMock(return_value=[user]))
        monkeypatch.setattr(
            main.db,
            "get_pending_digest",
            AsyncMock(return_value={"articles": [{"article_id": "a1"}]}),
        )
        save_pending = AsyncMock()
        monkeypatch.setattr(main.db, "save_pending_digest", save_pending)

        result = asyncio.run(main.prepare_digests(object()))

        assert result["prepared"] == 0
        save_pending.assert_not_awaited()
