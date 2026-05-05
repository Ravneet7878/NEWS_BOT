"""Unit tests for Telegram command, onboarding, admin, and feedback handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler

from shared.models import InviteCode, OnboardingState
from tests.conftest import FakeBot, FakeCallbackUpdate, FakeUpdate, fake_context, make_user


class TestOnboardingHelpers:
    def test_parse_hour_accepts_common_formats_and_timezones(self) -> None:
        from api.handlers import onboarding

        assert onboarding._parse_hour("7") == (7, "Asia/Kolkata")
        assert onboarding._parse_hour("7am IST") == (7, "Asia/Kolkata")
        assert onboarding._parse_hour("12am UTC") == (0, "UTC")
        assert onboarding._parse_hour("12pm EST") == (12, "America/New_York")
        assert onboarding._parse_hour("19:00") == (19, "Asia/Kolkata")
        assert onboarding._parse_hour("24:00") is None
        assert onboarding._parse_hour("nope") is None

    def test_parse_time_returns_minute_component(self) -> None:
        from api.handlers import onboarding

        assert onboarding._parse_time("7") == (7, 0, "Asia/Kolkata")
        assert onboarding._parse_time("7am IST") == (7, 0, "Asia/Kolkata")
        assert onboarding._parse_time("8:30 PM EST") == (20, 30, "America/New_York")
        assert onboarding._parse_time("1:00 AM IST") == (1, 0, "Asia/Kolkata")
        assert onboarding._parse_time("nope") is None

    def test_local_to_utc_hm_ist_offset(self) -> None:
        from api.handlers import onboarding

        utc_hour, utc_minute = onboarding._local_to_utc_hm(1, 0, "Asia/Kolkata")
        assert utc_hour == 19
        assert utc_minute == 30

    def test_local_to_utc_hour_falls_back_for_unknown_timezone(self) -> None:
        from api.handlers import onboarding

        assert 0 <= onboarding._local_to_utc_hour(7, "Not/AZone") <= 23


class TestOnboardingHandlers:
    def test_start_requires_invite_code(self, monkeypatch) -> None:
        from api.handlers import onboarding

        monkeypatch.setattr(onboarding.db, "get_user", AsyncMock(return_value=None))
        update = FakeUpdate(user_id=123)

        result = asyncio.run(onboarding.handle_start(update, fake_context()))

        assert result == ConversationHandler.END
        assert "invite link" in update.message.replies[-1]["text"]

    def test_start_rejects_missing_used_and_expired_invites(self, monkeypatch) -> None:
        from api.handlers import onboarding

        update = FakeUpdate(user_id=123)
        monkeypatch.setattr(onboarding.db, "get_user", AsyncMock(return_value=None))

        monkeypatch.setattr(
            onboarding.db, "claim_invite_code_and_create_user",
            AsyncMock(side_effect=ValueError("not_found")),
        )
        assert asyncio.run(onboarding.handle_start(update, fake_context("BAD"))) == ConversationHandler.END
        assert "doesn't exist" in update.message.replies[-1]["text"]

        monkeypatch.setattr(
            onboarding.db, "claim_invite_code_and_create_user",
            AsyncMock(side_effect=ValueError("already_used")),
        )
        assert asyncio.run(onboarding.handle_start(update, fake_context("USED"))) == ConversationHandler.END
        assert "already been used" in update.message.replies[-1]["text"]

        monkeypatch.setattr(
            onboarding.db, "claim_invite_code_and_create_user",
            AsyncMock(side_effect=ValueError("expired")),
        )
        assert asyncio.run(onboarding.handle_start(update, fake_context("OLD"))) == ConversationHandler.END
        assert "expired" in update.message.replies[-1]["text"]

    def test_start_creates_user_and_marks_code_used(self, monkeypatch) -> None:
        from api.handlers import onboarding

        claim = AsyncMock()
        monkeypatch.setattr(onboarding.db, "get_user", AsyncMock(return_value=None))
        monkeypatch.setattr(onboarding.db, "claim_invite_code_and_create_user", claim)
        update = FakeUpdate(user_id=123, first_name="Ada")

        result = asyncio.run(onboarding.handle_start(update, fake_context("join")))

        assert result == onboarding.AWAITING_TOPICS
        code_arg, tid_arg, user_arg = claim.await_args.args
        assert code_arg == "JOIN"
        assert tid_arg == "123"
        assert user_arg.telegram_id == "123"
        assert user_arg.invite_code_used == "JOIN"
        assert "What topics" in update.message.replies[-1]["text"]

    def test_start_existing_done_user_ends_conversation(self, monkeypatch) -> None:
        from api.handlers import onboarding

        monkeypatch.setattr(onboarding.db, "get_user", AsyncMock(return_value=make_user(first_name="Ada")))
        update = FakeUpdate(user_id=123)

        result = asyncio.run(onboarding.handle_start(update, fake_context("JOIN")))

        assert result == ConversationHandler.END
        assert "already registered" in update.message.replies[-1]["text"]

    def test_topics_input_updates_user_and_prompts_time(self, monkeypatch) -> None:
        from api.handlers import onboarding

        update_user = AsyncMock()
        monkeypatch.setattr(onboarding.db, "update_user", update_user)
        update = FakeUpdate("Tech, Finance", user_id=123)

        result = asyncio.run(onboarding.handle_topics_input(update, fake_context()))

        assert result == onboarding.AWAITING_TIME
        update_user.assert_awaited_once()
        assert update_user.await_args.kwargs["topics"] == ["Tech", "Finance"]
        assert update_user.await_args.kwargs["topic_weights"] == {"Tech": 1.0, "Finance": 1.0}
        assert "What time" in update.message.replies[-1]["text"]

    def test_topics_input_policy_error_stays_in_topics_state(self) -> None:
        from api.handlers import onboarding

        update = FakeUpdate("https://bad.example", user_id=123)

        result = asyncio.run(onboarding.handle_topics_input(update, fake_context()))

        assert result == onboarding.AWAITING_TOPICS
        assert "plain news subjects" in update.message.replies[-1]["text"]

    def test_time_input_rejects_bad_time_and_accepts_good_time(self, monkeypatch) -> None:
        from api.handlers import onboarding

        update_user = AsyncMock()
        monkeypatch.setattr(onboarding.db, "update_user", update_user)

        bad = FakeUpdate("tomorrow morning", user_id=123)
        assert asyncio.run(onboarding.handle_time_input(bad, fake_context())) == onboarding.AWAITING_TIME
        assert "couldn't understand" in bad.message.replies[-1]["text"]

        good = FakeUpdate("7am UTC", user_id=123)
        assert asyncio.run(onboarding.handle_time_input(good, fake_context())) == ConversationHandler.END
        update_user.assert_awaited_once()
        assert update_user.await_args.kwargs["delivery_hour_utc"] == 7
        assert update_user.await_args.kwargs["delivery_minute_utc"] == 0
        assert update_user.await_args.kwargs["delivery_tz"] == "UTC"
        assert update_user.await_args.kwargs["is_active"] is True


class TestUserCommands:
    def test_pause_resume_and_delete_require_registered_user(self, monkeypatch) -> None:
        from api.handlers import commands

        monkeypatch.setattr(commands.db, "get_user", AsyncMock(return_value=None))
        update = FakeUpdate(user_id=123)

        asyncio.run(commands.handle_pause(update, fake_context()))

        assert "not registered" in update.message.replies[-1]["text"]

    def test_pause_resume_and_confirmdelete_update_registered_user(self, monkeypatch) -> None:
        from api.handlers import commands

        monkeypatch.setattr(commands.db, "get_user", AsyncMock(return_value=make_user()))
        update_user = AsyncMock()
        delete_user = AsyncMock()
        monkeypatch.setattr(commands.db, "update_user", update_user)
        monkeypatch.setattr(commands.db, "delete_user_data", delete_user)

        pause = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_pause(pause, fake_context()))
        update_user.assert_awaited_with("123", is_paused=True)

        resume = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_resume(resume, fake_context()))
        update_user.assert_awaited_with("123", is_paused=False)

        delete = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_confirmdelete(delete, fake_context()))
        delete_user.assert_awaited_once_with("123")  # now calls delete_user_data

    def test_topics_command_preserves_existing_weights_and_validates_usage(self, monkeypatch) -> None:
        from api.handlers import commands

        update_user = AsyncMock()
        monkeypatch.setattr(
            commands.db,
            "get_user",
            AsyncMock(return_value=make_user(topic_weights={"Tech": 2.0, "Finance": 0.7})),
        )
        monkeypatch.setattr(commands.db, "update_user", update_user)

        missing = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_topics(missing, fake_context()))
        assert "Usage" in missing.message.replies[-1]["text"]

        invalid = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_topics(invalid, fake_context("http://bad.example")))
        assert "plain news subjects" in invalid.message.replies[-1]["text"]

        valid = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_topics(valid, fake_context("Tech,", "Climate")))
        update_user.assert_awaited_with("123", topics=["Tech", "Climate"], topic_weights={"Tech": 2.0, "Climate": 1.0})
        assert "Topics updated" in valid.message.replies[-1]["text"]

    def test_time_command_can_activate_inactive_user_with_topics(self, monkeypatch) -> None:
        from api.handlers import commands

        user = make_user(is_active=False, topics=["Tech"], onboarding_state=OnboardingState.AWAITING_TIME)
        update_user = AsyncMock()
        monkeypatch.setattr(commands.db, "get_user", AsyncMock(return_value=user))
        monkeypatch.setattr(commands.db, "update_user", update_user)

        update = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_time(update, fake_context("7am", "UTC")))

        assert update_user.await_args.kwargs["delivery_hour_utc"] == 7
        assert update_user.await_args.kwargs["delivery_minute_utc"] == 0
        assert update_user.await_args.kwargs["is_active"] is True
        assert update_user.await_args.kwargs["onboarding_state"] == OnboardingState.DONE

    def test_status_formats_user_settings_and_help_replies(self, monkeypatch) -> None:
        from api.handlers import commands

        user = make_user(last_digest_sent=datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc), total_digests_sent=3)
        monkeypatch.setattr(commands.db, "get_user", AsyncMock(return_value=user))

        status = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_status(status, fake_context()))
        assert "Your Status" in status.message.replies[-1]["text"]
        assert "Total digests sent: 3" in status.message.replies[-1]["text"]

        help_update = FakeUpdate(user_id=123)
        asyncio.run(commands.handle_help(help_update, fake_context()))
        assert "/status" in help_update.message.replies[-1]["text"]


class TestAdminHandlers:
    def test_non_admin_commands_are_ignored(self, monkeypatch) -> None:
        from api.handlers import admin

        update = FakeUpdate(user_id=999)

        asyncio.run(admin.handle_gencode(update, fake_context("1")))

        assert update.message.replies == []

    def test_gencode_validates_count_and_creates_codes(self, monkeypatch) -> None:
        from api.handlers import admin

        create_invite_code = AsyncMock()
        monkeypatch.setattr(admin.db, "create_invite_code", create_invite_code)
        monkeypatch.setattr(admin.secrets, "token_urlsafe", lambda n: "abc123")

        bad = FakeUpdate(user_id=100)
        asyncio.run(admin.handle_gencode(bad, fake_context("51")))
        assert "Usage" in bad.message.replies[-1]["text"]

        good = FakeUpdate(user_id=100)
        asyncio.run(admin.handle_gencode(good, fake_context("2")))

        assert create_invite_code.await_count == 2
        assert "ABC123" in good.message.replies[-1]["text"]
        assert good.message.replies[-1]["parse_mode"] == "Markdown"

    def test_users_summary_counts_user_states(self, monkeypatch) -> None:
        from api.handlers import admin

        users = [
            make_user(is_active=True, is_paused=False, onboarding_state=OnboardingState.DONE),
            make_user(telegram_id="2", is_active=True, is_paused=True, onboarding_state=OnboardingState.DONE),
            make_user(telegram_id="3", is_active=False, onboarding_state=OnboardingState.AWAITING_TOPICS),
        ]
        monkeypatch.setattr(admin.db, "get_all_users", AsyncMock(return_value=users))
        update = FakeUpdate(user_id=100)

        asyncio.run(admin.handle_users(update, fake_context()))

        text = update.message.replies[-1]["text"]
        assert "Total:               3" in text
        assert "Active:              1" in text
        assert "Paused:              1" in text
        assert "Onboarding pending:  1" in text

    def test_broadcast_validates_message_and_counts_failures(self, monkeypatch) -> None:
        from api.handlers import admin

        sleep = AsyncMock()
        monkeypatch.setattr(admin.asyncio, "sleep", sleep)
        monkeypatch.setattr(
            admin.db,
            "get_all_active_users",
            AsyncMock(return_value=[make_user(telegram_id="1"), make_user(telegram_id="2")]),
        )

        missing = FakeUpdate(user_id=100)
        asyncio.run(admin.handle_broadcast(missing, fake_context()))
        assert "Usage" in missing.message.replies[-1]["text"]

        unsafe = FakeUpdate(user_id=100)
        asyncio.run(admin.handle_broadcast(unsafe, fake_context("how", "to", "make", "a", "bomb")))
        assert "disallowed" in unsafe.message.replies[-1]["text"]

        bot = FakeBot(fail_for={"2"})
        update = FakeUpdate(user_id=100, bot=bot)
        asyncio.run(admin.handle_broadcast(update, fake_context("hello")))

        assert len(bot.sent) == 1
        assert "1 sent, 1 failed" in update.message.replies[-1]["text"]
        assert sleep.await_count == 2


class TestFeedbackHandler:
    def test_feedback_rejects_unknown_actions_and_types(self) -> None:
        from api.handlers import feedback

        bad_action = FakeCallbackUpdate("other:more:Tech:abc")
        asyncio.run(feedback.handle_feedback(bad_action, fake_context()))
        assert bad_action.callback_query.answers == ["Unknown action."]

        bad_type = FakeCallbackUpdate("feedback:maybe:Tech:abc")
        asyncio.run(feedback.handle_feedback(bad_type, fake_context()))
        assert bad_type.callback_query.answers == ["Unknown feedback type."]

    def test_feedback_ignores_topics_not_owned_by_user(self, monkeypatch) -> None:
        from api.handlers import feedback

        monkeypatch.setattr(feedback.db, "get_user", AsyncMock(return_value=make_user(topics=["Tech"])))
        update = FakeCallbackUpdate("feedback:more:Sports:abc")

        asyncio.run(feedback.handle_feedback(update, fake_context()))

        assert update.callback_query.answers == [None]

    def test_feedback_updates_weights_and_reaction(self, monkeypatch) -> None:
        from api.handlers import feedback

        update_weights = AsyncMock()
        record_reaction = AsyncMock()
        monkeypatch.setattr(feedback.db, "get_user", AsyncMock(return_value=make_user(topics=["Tech"])))
        monkeypatch.setattr(feedback.db, "update_topic_weights", update_weights)
        monkeypatch.setattr(feedback.db, "record_article_reaction", record_reaction)
        update = FakeCallbackUpdate("feedback:less:Tech:abcdef12")

        asyncio.run(feedback.handle_feedback(update, fake_context()))

        update_weights.assert_awaited_once_with("123", "Tech", -0.1)
        record_reaction.assert_awaited_once_with("123", "Tech", "abcdef12", "less")
        assert update.callback_query.answers == ["Got it! Adjusting your feed."]
