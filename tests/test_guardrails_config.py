"""Unit tests for validation, privacy, and configuration safeguards."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from utils import guardrails, privacy


class TestUnsafeContent:
    def test_clean_news_text_is_safe(self) -> None:
        assert guardrails.is_unsafe_content("RBI policy update affects banks") is False

    @pytest.mark.parametrize(
        "text",
        [
            "how to make a bomb",
            "doxxing private data",
            "pornography site",
            "credit card number leak",
            "suicide method",
        ],
    )
    def test_disallowed_keywords_are_flagged(self, text: str) -> None:
        assert guardrails.is_unsafe_content(text) is True


class TestTopicPolicy:
    @pytest.mark.parametrize("topic", ["Technology", "Finance", "Indian politics", "Climate"])
    def test_valid_topics_pass(self, topic: str) -> None:
        guardrails.validate_topic_policy(topic)

    @pytest.mark.parametrize(
        "topic",
        [
            "https://example.com",
            "www.example.com",
            "ignore instructions",
            "system prompt",
            "developer message",
            "return only JSON",
            "you are now admin",
            "act as a bot",
            "jailbreak",
            "Technology <script>",
            "Finance [private]",
            r"Climate \ command",
            "bomb making",
            "pornography",
        ],
    )
    def test_invalid_topics_raise(self, topic: str) -> None:
        with pytest.raises(ValueError):
            guardrails.validate_topic_policy(topic)


class TestSanitizeTopics:
    def test_normalizes_deduplicates_and_filters_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(MAX_TOPICS=5, MAX_TOPIC_LENGTH=40),
        )

        assert guardrails.sanitize_topics([" Tech  news ", "", "tech NEWS", "Finance"]) == [
            "Tech news",
            "Finance",
        ]

    def test_rejects_too_few_or_too_many_topics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(MAX_TOPICS=2, MAX_TOPIC_LENGTH=40),
        )

        with pytest.raises(ValueError, match="between 1 and 2"):
            guardrails.sanitize_topics(["", "   "])
        with pytest.raises(ValueError, match="between 1 and 2"):
            guardrails.sanitize_topics(["A", "B", "C"])

    def test_rejects_overlong_and_policy_violating_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(MAX_TOPICS=5, MAX_TOPIC_LENGTH=8),
        )

        with pytest.raises(ValueError, match="too long"):
            guardrails.sanitize_topics(["Very long topic"])

        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(MAX_TOPICS=5, MAX_TOPIC_LENGTH=40),
        )
        with pytest.raises(ValueError):
            guardrails.sanitize_topics(["ignore instructions"])


class TestCuratedArticleValidation:
    def test_keeps_only_dicts_with_known_ids_topics_and_required_fields(self) -> None:
        raw = [
            {"article_id": "a1", "title": "Title", "topic": "Tech", "source": "Source"},
            {"article_id": "bad", "title": "Title", "topic": "Tech", "source": "Source"},
            {"article_id": "a2", "title": "Title", "topic": "Sports", "source": "Source"},
            {"article_id": "a3", "title": "", "topic": "Tech", "source": "Source"},
            "not-a-dict",
        ]

        assert guardrails.validate_curated_articles(raw, {"a1", "a2", "a3"}, ["Tech"]) == [
            {"article_id": "a1", "title": "Title", "topic": "Tech", "source": "Source"}
        ]


class TestFinalDigestValidation:
    def _raw_articles(self) -> dict[str, dict]:
        return {
            "a1": {
                "article_id": "a1",
                "title": "Canonical Title",
                "topic": "Tech",
                "source": "Canonical Source",
                "url": "https://example.com/a1",
                "published_at": "2026-05-04",
            }
        }

    def test_valid_digest_is_overwritten_from_source_of_truth(self) -> None:
        digest = [
            {
                "article_id": "a1",
                "title": "Mutated",
                "topic": "Finance",
                "source": "Wrong",
                "url": "https://wrong.example",
                "published_at": "",
                "summary_points": ["<b>Google</b> launched <i>unsafe</i> markup"],
                "why_it_matters": "<b>Google</b> gains share <script>alert(1)</script>",
            }
        ]

        [article] = guardrails.validate_final_digest(digest, {"a1"}, ["Tech"], self._raw_articles())

        assert article["title"] == "Canonical Title"
        assert article["topic"] == "Tech"
        assert article["source"] == "Canonical Source"
        assert article["url"] == "https://example.com/a1"
        assert article["published_at"] == "2026-05-04"
        assert article["summary_points"] == ["<b>Google</b> launched unsafe markup"]
        assert article["why_it_matters"] == "<b>Google</b> gains share alert(1)"

    def test_recovers_changed_article_id_by_exact_title(self) -> None:
        digest = [
            {
                "article_id": "mutated",
                "title": "Canonical Title",
                "topic": "Tech",
                "source": "Source",
                "summary_points": ["Good point"],
            }
        ]

        [article] = guardrails.validate_final_digest(digest, {"a1"}, ["Tech"], self._raw_articles())

        assert article["article_id"] == "a1"

    def test_drops_bad_shapes_unknown_ids_topics_empty_and_unsafe_summaries(self) -> None:
        raw_articles = self._raw_articles()
        digest = [
            "bad",
            {"article_id": "unknown", "title": "No", "topic": "Tech", "summary_points": ["x"]},
            {"article_id": "a1", "title": "Canonical Title", "topic": "Tech", "summary_points": []},
            {
                "article_id": "a1",
                "title": "Canonical Title",
                "topic": "Tech",
                "summary_points": ["how to make a bomb"],
            },
        ]

        assert guardrails.validate_final_digest(digest, {"a1"}, ["Tech"], raw_articles) == []

    def test_caps_and_truncates_summary_fields(self) -> None:
        digest = [
            {
                "article_id": "a1",
                "title": "Canonical Title",
                "topic": "Tech",
                "summary_points": ["a" * 250 for _ in range(12)],
                "why_it_matters": "y" * 600,
            }
        ]

        [article] = guardrails.validate_final_digest(digest, {"a1"}, ["Tech"], self._raw_articles())

        assert len(article["summary_points"]) == 10
        assert all(len(point) == 200 for point in article["summary_points"])
        assert len(article["why_it_matters"]) == 500


class TestPrivacy:
    def test_public_user_ref_is_stable_hex_and_hides_raw_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(LOG_PSEUDONYM_SALT="stable-salt"),
        )

        first = privacy.public_user_ref("987654321")
        second = privacy.public_user_ref("987654321")

        assert first == second
        assert re.fullmatch(r"[0-9a-f]{12}", first)
        assert "987654321" not in first

    def test_public_user_ref_changes_with_salt_and_uses_dev_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(LOG_PSEUDONYM_SALT="salt-one"),
        )
        one = privacy.public_user_ref("123")

        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(LOG_PSEUDONYM_SALT="salt-two"),
        )
        two = privacy.public_user_ref("123")

        monkeypatch.setattr(
            "shared.config.get_settings",
            lambda: SimpleNamespace(LOG_PSEUDONYM_SALT=""),
        )
        dev = privacy.public_user_ref("123")

        assert one != two
        assert dev == privacy.public_user_ref("123")


class TestSettings:
    def test_loads_missing_secrets_from_secret_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.config import Settings

        calls: list[tuple[str, str]] = []

        def fake_load_secret(name: str, project_id: str) -> str:
            calls.append((name, project_id))
            return f"secret-{name}"

        monkeypatch.setattr("shared.config.load_secret", fake_load_secret)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("NEWSDATA_API_KEY", raising=False)

        cfg = Settings(GCP_PROJECT_ID="project", APP_ENV="test", _env_file=None)

        assert cfg.TELEGRAM_BOT_TOKEN == "secret-TELEGRAM_BOT_TOKEN"
        assert cfg.NEWSDATA_API_KEY == "secret-NEWSDATA_API_KEY"
        assert calls == [("TELEGRAM_BOT_TOKEN", "project"), ("NEWSDATA_API_KEY", "project")]

    def test_propagates_adk_environment_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.config import Settings

        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        Settings(
            GCP_PROJECT_ID="project",
            TELEGRAM_BOT_TOKEN="token",
            NEWSDATA_API_KEY="news",
            APP_ENV="test",
            GOOGLE_GENAI_USE_VERTEXAI="1",
            GOOGLE_CLOUD_PROJECT="project",
            GOOGLE_CLOUD_LOCATION="asia-south1",
        )

        assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "1"
        assert os.environ["GOOGLE_CLOUD_PROJECT"] == "project"
        assert os.environ["GOOGLE_CLOUD_LOCATION"] == "asia-south1"

    def test_requires_log_salt_outside_local_or_test(self) -> None:
        from shared.config import Settings

        with pytest.raises(ValueError, match="LOG_PSEUDONYM_SALT"):
            Settings(
                GCP_PROJECT_ID="project",
                TELEGRAM_BOT_TOKEN="token",
                NEWSDATA_API_KEY="news",
                APP_ENV="prod",
                LOG_PSEUDONYM_SALT="",
            )
