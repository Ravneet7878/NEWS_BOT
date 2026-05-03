"""Pydantic data models shared across the api and worker services."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class OnboardingState(StrEnum):
    AWAITING_TOPICS = "AWAITING_TOPICS"
    AWAITING_TIME = "AWAITING_TIME"
    DONE = "DONE"


class User(BaseModel):
    telegram_id: str
    first_name: str
    username: str | None = None
    is_active: bool = False
    is_paused: bool = False
    invite_code_used: str
    topics: list[str] = []
    # Clamped [0.5, 3.0]; normalised so sum == len(topics)
    topic_weights: dict[str, float] = {}
    delivery_hour_utc: int = 1
    delivery_tz: str = "Asia/Kolkata"
    onboarding_state: OnboardingState = OnboardingState.AWAITING_TOPICS
    created_at: datetime
    last_digest_sent: datetime | None = None
    total_digests_sent: int = 0


class InviteCode(BaseModel):
    code: str
    is_used: bool = False
    used_by: str | None = None
    used_at: datetime | None = None
    created_at: datetime
    expires_at: datetime | None = None
