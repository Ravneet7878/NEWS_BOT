"""Shared unit-test fakes and safe environment defaults."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("VERTEX_AI_LOCATION", "asia-south1")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "100")
os.environ.setdefault("WEBHOOK_SECRET_TOKEN", "secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("NEWSDATA_API_KEY", "news-key")
os.environ.setdefault("LOG_PSEUDONYM_SALT", "test-salt")


def _filter_known_dependency_warnings() -> None:
    """Hide only known dependency import warnings, even when pytest runs with -W default."""
    warnings.filterwarnings(
        "ignore",
        message=r"authlib\.jose module is deprecated, please use joserfc instead\.",
        category=Warning,
        module=r"authlib\._joserfc_helpers",
    )
    warnings.filterwarnings(
        "ignore",
        message=r"\[EXPERIMENTAL\] feature FeatureName\.PLUGGABLE_AUTH is enabled\.",
        category=UserWarning,
        module=r"google\.adk\.features\._feature_decorator",
    )


def pytest_configure() -> None:
    _filter_known_dependency_warnings()


_filter_known_dependency_warnings()


@pytest.fixture(autouse=True)
def known_dependency_warning_filters():
    _filter_known_dependency_warnings()


@dataclass
class FakeTelegramUser:
    id: int = 123
    first_name: str = "Test"
    username: str | None = "tester"


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[dict[str, Any]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append({"text": text, **kwargs})


class FakeBot:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, **kwargs: Any) -> None:
        if str(kwargs.get("chat_id")) in self.fail_for:
            raise RuntimeError("send failed")
        self.sent.append(kwargs)


class FakeUpdate:
    def __init__(
        self,
        text: str | None = None,
        user_id: int = 123,
        first_name: str = "Test",
        username: str | None = "tester",
        bot: FakeBot | None = None,
    ) -> None:
        self.effective_user = FakeTelegramUser(user_id, first_name, username)
        self.message = FakeMessage(text)
        self._bot = bot or FakeBot()
        self.callback_query = None

    def get_bot(self) -> FakeBot:
        return self._bot


class FakeCallbackQuery:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.data = data
        self.from_user = FakeTelegramUser(user_id)
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)


class FakeCallbackUpdate:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.callback_query = FakeCallbackQuery(data, user_id)
        self.effective_user = None
        self.message = None


def fake_context(*args: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))


class FakeDoc:
    def __init__(self, data: dict[str, Any] | None = None, exists: bool = True, doc_id: str = "doc") -> None:
        self._data = data or {}
        self.exists = exists
        self.id = doc_id

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class FakeDocumentRef:
    def __init__(self, doc_id: str, store: dict[str, dict[str, Any]]) -> None:
        self.id = doc_id
        self.store = store
        self.set_payloads: list[dict[str, Any]] = []
        self.update_payloads: list[dict[str, Any]] = []
        self.deleted = False

    async def get(self, **kwargs: Any) -> FakeDoc:
        if self.id not in self.store:
            return FakeDoc(exists=False, doc_id=self.id)
        return FakeDoc(self.store[self.id], exists=True, doc_id=self.id)

    async def set(self, payload: dict[str, Any]) -> None:
        self.set_payloads.append(payload)
        self.store[self.id] = dict(payload)

    async def update(self, payload: dict[str, Any]) -> None:
        self.update_payloads.append(payload)
        self.store.setdefault(self.id, {}).update(payload)

    async def delete(self) -> None:
        self.deleted = True
        self.store.pop(self.id, None)


class FakeCollection:
    def __init__(self, name: str, store: dict[str, dict[str, Any]]) -> None:
        self.name = name
        self.store = store
        self.filters: list[Any] = []
        self.refs: dict[str, FakeDocumentRef] = {}

    def document(self, doc_id: str) -> FakeDocumentRef:
        self.refs.setdefault(doc_id, FakeDocumentRef(doc_id, self.store))
        return self.refs[doc_id]

    def where(self, *, filter: Any) -> "FakeCollection":
        self.filters.append(filter)
        return self

    async def _stream(self):
        for doc_id, data in self.store.items():
            yield FakeDoc(data, exists=True, doc_id=doc_id)

    def stream(self):
        return self._stream()


class FakeTransaction:
    def __init__(self) -> None:
        self._id: bytes | None = None
        self._read_only = False
        self._max_attempts = 1
        self.committed = False
        self.rolled_back = False

    def _clean_up(self) -> None:
        self._id = None

    async def _begin(self, retry_id: bytes | None = None) -> None:
        self._id = retry_id or b"fake-transaction"

    async def _commit(self) -> None:
        self.committed = True

    async def _rollback(self) -> None:
        self.rolled_back = True

    def update(self, reference: FakeDocumentRef, field_updates: dict[str, Any]) -> None:
        reference.store.setdefault(reference.id, {}).update(field_updates)

    def set(self, reference: FakeDocumentRef, document_data: dict[str, Any]) -> None:
        reference.store[reference.id] = dict(document_data)


class FakeFirestore:
    def __init__(self) -> None:
        self.stores: dict[str, dict[str, dict[str, Any]]] = {}
        self.collections: dict[str, FakeCollection] = {}
        self.transactions: list[FakeTransaction] = []

    def collection(self, name: str) -> FakeCollection:
        self.stores.setdefault(name, {})
        self.collections.setdefault(name, FakeCollection(name, self.stores[name]))
        return self.collections[name]

    def transaction(self) -> FakeTransaction:
        transaction = FakeTransaction()
        self.transactions.append(transaction)
        return transaction


def make_user(**overrides: Any):
    from shared.models import OnboardingState, User

    defaults = {
        "telegram_id": "123",
        "first_name": "Test",
        "username": "tester",
        "is_active": True,
        "is_paused": False,
        "invite_code_used": "CODE",
        "topics": ["Tech", "Finance"],
        "topic_weights": {"Tech": 1.0, "Finance": 1.0},
        "delivery_hour_utc": 1,
        "delivery_minute_utc": 0,
        "delivery_tz": "Asia/Kolkata",
        "onboarding_state": OnboardingState.DONE,
        "created_at": datetime(2026, 1, 1),
        "last_digest_sent": None,
        "total_digests_sent": 0,
    }
    defaults.update(overrides)
    return User(**defaults)


@pytest.fixture
def fake_db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def async_mock() -> type[AsyncMock]:
    return AsyncMock
