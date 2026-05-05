"""Unit tests for Firestore helper and cache behavior using in-memory fakes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import FakeDoc, FakeFirestore, make_user


class TestDatabaseUserAndInviteHelpers:
    def test_user_crud_and_invite_helpers_use_expected_documents(self, fake_db: FakeFirestore, monkeypatch) -> None:
        import shared.database as db

        monkeypatch.setattr(db, "_db", fake_db)
        user = make_user(telegram_id="123")

        asyncio.run(db.create_user(user))
        assert fake_db.stores["users"]["123"]["telegram_id"] == "123"

        loaded = asyncio.run(db.get_user("123"))
        assert loaded == user

        asyncio.run(db.update_user("123", is_paused=True))
        assert fake_db.stores["users"]["123"]["is_paused"] is True

        asyncio.run(db.delete_user("123"))
        assert "123" not in fake_db.stores["users"]

        asyncio.run(db.create_invite_code("JOIN"))
        assert fake_db.stores["invite_codes"]["JOIN"]["code"] == "JOIN"

        invite = asyncio.run(db.get_invite_code("JOIN"))
        assert invite.code == "JOIN"

        asyncio.run(db.mark_code_used("JOIN", "123"))
        assert fake_db.stores["invite_codes"]["JOIN"]["is_used"] is True
        assert fake_db.stores["invite_codes"]["JOIN"]["used_by"] == "123"

    def test_queries_skip_malformed_docs_and_apply_filters(self, fake_db: FakeFirestore, monkeypatch) -> None:
        import shared.database as db

        monkeypatch.setattr(db, "_db", fake_db)
        fake_db.collection("users")
        fake_db.stores["users"]["good"] = make_user(telegram_id="good").model_dump(mode="json")
        fake_db.stores["users"]["bad"] = {"telegram_id": "bad"}

        users = asyncio.run(db.get_active_users_for_hour(1))

        assert [u.telegram_id for u in users] == ["good"]
        assert len(fake_db.collections["users"].filters) == 3

        all_users = asyncio.run(db.get_all_users())
        assert [u.telegram_id for u in all_users] == ["good"]

        active_users = asyncio.run(db.get_all_active_users())
        assert [u.telegram_id for u in active_users] == ["good"]
        assert len(fake_db.collections["users"].filters) == 4

    def test_update_topic_weights_clamps_normalizes_and_ignores_missing_user(self, monkeypatch) -> None:
        import shared.database as db

        update_user = AsyncMock()
        user = make_user(
            telegram_id="123",
            topics=["Tech", "Finance"],
            topic_weights={"Tech": 2.9, "Finance": 0.6},
        )
        monkeypatch.setattr(db, "get_user", AsyncMock(return_value=user))
        monkeypatch.setattr(db, "update_user", update_user)

        asyncio.run(db.update_topic_weights("123", "Tech", 1.0))

        weights = update_user.await_args.kwargs["topic_weights"]
        assert weights["Tech"] <= 3.0
        assert weights["Finance"] >= 0.5
        assert set(weights) == {"Tech", "Finance"}

        update_user.reset_mock()
        monkeypatch.setattr(db, "get_user", AsyncMock(return_value=None))
        asyncio.run(db.update_topic_weights("missing", "Tech", 0.1))
        update_user.assert_not_awaited()


class TestDigestHistoryAndPending:
    def test_digest_history_doc_ids_and_payloads_strip_reactions(self, fake_db: FakeFirestore, monkeypatch) -> None:
        import shared.database as db

        monkeypatch.setattr(db, "_db", fake_db)
        monkeypatch.setattr(db, "public_user_ref", lambda telegram_id: f"ref-{telegram_id}")
        sent_at = datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc)
        articles = [
            {
                "article_id": "a1",
                "url": "https://example.com",
                "title": "Title",
                "topic": "Tech",
                "source": "Source",
                "published_at": "2026-05-04",
                "reaction": "more",
                "reacted_at": sent_at,
            }
        ]

        asyncio.run(db.save_user_digest_history("123", articles, sent_at))

        doc_id = "ref-123_2026-05-04"
        payload = fake_db.stores["user_digest_history"][doc_id]
        assert payload["user_ref"] == "ref-123"
        assert payload["articles"][0]["reaction"] is None
        assert payload["articles"][0]["reacted_at"] is None

    def test_record_article_reaction_patches_today_or_yesterday(self, fake_db: FakeFirestore, monkeypatch) -> None:
        import shared.database as db

        monkeypatch.setattr(db, "_db", fake_db)
        today = datetime.now(timezone.utc).date()
        doc_id = db._digest_history_doc_id("123", today)
        fake_db.collection("user_digest_history")
        fake_db.stores["user_digest_history"][doc_id] = {
            "articles": [{"article_id": "abcdef123", "topic": "Tech"}]
        }

        asyncio.run(db.record_article_reaction("123", "Tech", "abcdef", "more"))

        article = fake_db.stores["user_digest_history"][doc_id]["articles"][0]
        assert article["reaction"] == "more"
        assert isinstance(article["reacted_at"], datetime)

    def test_pending_digest_save_get_expire_and_mark_delivered(self, fake_db: FakeFirestore, monkeypatch) -> None:
        import shared.database as db

        monkeypatch.setattr(db, "_db", fake_db)
        monkeypatch.setattr(db, "public_user_ref", lambda telegram_id: f"ref-{telegram_id}")
        target_date = date(2026, 5, 4)

        asyncio.run(db.save_pending_digest("123", target_date, 7, [{"article_id": "a1"}]))

        doc_id = "ref-123_2026-05-04_07"
        assert fake_db.stores["pending_digests"][doc_id]["target_hour"] == 7

        pending = asyncio.run(db.get_pending_digest("123", target_date, 7))
        assert pending["articles"] == [{"article_id": "a1"}]

        fake_db.stores["pending_digests"][doc_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert asyncio.run(db.get_pending_digest("123", target_date, 7)) is None

        asyncio.run(db.mark_pending_delivered("123", target_date, 7))
        assert isinstance(fake_db.stores["pending_digests"][doc_id]["delivered_at"], datetime)


class TestNewsCache:
    def test_cache_key_is_stable_and_normalized(self) -> None:
        from worker.pipeline import news_cache

        assert news_cache.cache_key(" Tech ") == news_cache.cache_key("tech")
        assert news_cache.cache_key("Tech") != news_cache.cache_key("Finance")

    def test_get_cached_topic_handles_hit_miss_expiry_and_read_failure(self, fake_db: FakeFirestore, monkeypatch) -> None:
        from worker.pipeline import news_cache

        monkeypatch.setattr(news_cache, "_db", fake_db)
        key = "k"
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        fake_db.collection("news_cache")
        fake_db.stores["news_cache"][key] = {
            "topic": "Tech",
            "raw_articles": [{"title": "A"}],
            "expires_at": future,
        }

        assert asyncio.run(news_cache.get_cached_topic(key)) == {
            "topic": "Tech",
            "raw_articles": [{"title": "A"}],
        }

        fake_db.stores["news_cache"][key]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert asyncio.run(news_cache.get_cached_topic(key)) is None
        assert asyncio.run(news_cache.get_cached_topic("missing")) is None

        class BrokenDb:
            def collection(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(news_cache, "_db", BrokenDb())
        assert asyncio.run(news_cache.get_cached_topic(key)) is None

    def test_set_cached_topic_writes_ttl_and_swallows_failures(self, fake_db: FakeFirestore, monkeypatch) -> None:
        from worker.pipeline import news_cache

        monkeypatch.setattr(news_cache, "_db", fake_db)

        asyncio.run(news_cache.set_cached_topic("k", "Tech", [{"title": "A"}]))

        payload = fake_db.stores["news_cache"]["k"]
        assert payload["topic"] == "Tech"
        assert payload["raw_articles"] == [{"title": "A"}]
        assert payload["expires_at"] > payload["fetched_at"]

        class BrokenDb:
            def collection(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(news_cache, "_db", BrokenDb())
        asyncio.run(news_cache.set_cached_topic("k", "Tech", []))

    def test_as_aware_utc_accepts_firestore_timestamp_like_object(self) -> None:
        from worker.pipeline import news_cache

        ts = SimpleNamespace(seconds=1_700_000_000)
        assert news_cache._as_aware_utc(ts).tzinfo == timezone.utc
        with pytest.raises(TypeError):
            news_cache._as_aware_utc(object())


class TestLlmCache:
    def test_keys_are_versioned_and_normalized(self) -> None:
        from worker.pipeline import llm_cache

        assert llm_cache._curated_topic_key(" Tech ") == llm_cache._curated_topic_key("tech")
        assert llm_cache._article_summary_key("https://a") != llm_cache._article_summary_key("https://b")

    def test_expiry_helper_treats_missing_or_invalid_expiry_as_expired(self) -> None:
        from worker.pipeline import llm_cache

        assert llm_cache._is_expired({}) is True
        assert llm_cache._is_expired({"expires_at": object()}) is True
        assert llm_cache._is_expired({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}) is True
        assert llm_cache._is_expired({"expires_at": datetime.now(timezone.utc) + timedelta(seconds=60)}) is False

    def test_curated_topic_cache_hit_miss_set_and_failure(self, fake_db: FakeFirestore, monkeypatch) -> None:
        from worker.pipeline import llm_cache

        monkeypatch.setattr(llm_cache, "_db", fake_db)
        key = llm_cache._curated_topic_key("Tech")
        fake_db.collection("curated_topics_v1")
        fake_db.stores["curated_topics_v1"][key] = {
            "curated_articles": [{"article_id": "a1"}],
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

        assert asyncio.run(llm_cache.get_cached_curated_topic("Tech")) == [{"article_id": "a1"}]

        fake_db.stores["curated_topics_v1"][key]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert asyncio.run(llm_cache.get_cached_curated_topic("Tech")) is None

        asyncio.run(llm_cache.set_cached_curated_topic("Finance", [{"article_id": "f1"}]))
        finance_key = llm_cache._curated_topic_key("Finance")
        assert fake_db.stores["curated_topics_v1"][finance_key]["curated_articles"] == [{"article_id": "f1"}]

        class BrokenDb:
            def collection(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(llm_cache, "_db", BrokenDb())
        assert asyncio.run(llm_cache.get_cached_curated_topic("Tech")) is None
        asyncio.run(llm_cache.set_cached_curated_topic("Tech", []))

    def test_article_summary_cache_filters_metadata_on_hit(self, fake_db: FakeFirestore, monkeypatch) -> None:
        from worker.pipeline import llm_cache

        monkeypatch.setattr(llm_cache, "_db", fake_db)
        key = llm_cache._article_summary_key("https://example.com")
        fake_db.collection("article_summaries_v1")
        fake_db.stores["article_summaries_v1"][key] = {
            "title": "Title",
            "url": "https://example.com",
            "summarised_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

        assert asyncio.run(llm_cache.get_cached_article_summary("https://example.com")) == {
            "title": "Title",
            "url": "https://example.com",
        }

        asyncio.run(llm_cache.set_cached_article_summary("https://new.example", {"title": "New"}))
        new_key = llm_cache._article_summary_key("https://new.example")
        assert fake_db.stores["article_summaries_v1"][new_key]["url"] == "https://new.example"
