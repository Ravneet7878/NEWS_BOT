"""Application configuration loaded from environment variables and GCP Secret Manager."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from utils.logging import get_logger

logger = get_logger(__name__)


def load_secret(name: str, project_id: str) -> str:
    """Fetch the latest version of a secret from GCP Secret Manager."""
    try:
        from google.cloud import secretmanager  # type: ignore[import-untyped]

        client = secretmanager.SecretManagerServiceClient()
        resource = f"projects/{project_id}/secrets/{name}/versions/latest"
        response = client.access_secret_version(name=resource)
        return response.payload.data.decode("utf-8")
    except Exception as exc:
        logger.warning("Failed to load secret %s from Secret Manager: %s", name, exc)
        return ""


class Settings(BaseSettings):
    GCP_PROJECT_ID: str
    VERTEX_AI_LOCATION: str = "asia-south1"
    ADMIN_TELEGRAM_ID: str = ""
    WEBHOOK_SECRET_TOKEN: str = ""

    # Populated in model_post_init from Secret Manager (or .env for local dev)
    TELEGRAM_BOT_TOKEN: str = ""
    NEWSDATA_API_KEY: str = ""

    DIGEST_MAX_ARTICLES: int = 7
    MAX_TOPICS: int = 7

    # News cache (Firestore-backed, cross-invocation)
    NEWS_CACHE_TTL_SECONDS: int = 86400         # 24 hours
    NEWS_CACHE_KEY_VERSION: str = "v1"
    DIGEST_HISTORY_TTL_SECONDS: int = 604800    # 7 days

    # LLM caches (cross-user, Firestore-backed)
    CURATOR_VERSION: str = "v1"
    SUMMARISER_VERSION: str = "v1"
    CURATED_TOPIC_TTL_SECONDS: int = 3600       # 1 hour
    ARTICLE_SUMMARY_TTL_SECONDS: int = 86400    # 24 hours
    PENDING_DIGEST_TTL_SECONDS: int = 86400     # 24 hours

    # Concurrency caps
    NEWS_FETCH_CONCURRENCY: int = 5
    CITATION_CHECK_CONCURRENCY: int = 10
    USER_PROCESS_CONCURRENCY: int = 10
    SUMMARISER_BATCH_SIZE: int = 3
    SUMMARISER_CONCURRENCY: int = 3

    # Retries
    RETRY_MAX_ATTEMPTS: int = 3
    RETRY_BACKOFF_BASE_SECONDS: float = 0.5
    RETRY_BACKOFF_MAX_SECONDS: float = 10.0

    # Privacy & guardrails
    LOG_PSEUDONYM_SALT: str = ""   # HMAC key for log pseudonymization; required in non-local envs
    MAX_TOPIC_LENGTH: int = 40
    MAX_BROADCAST_LENGTH: int = 4000  # Telegram hard limit is 4096

    # Deployment environment: "local" or "test" disables the salt requirement
    APP_ENV: str = "local"

    # ADK backend selection — set to "1" locally to use Vertex AI instead of Gemini API
    GOOGLE_GENAI_USE_VERTEXAI: str = ""
    GOOGLE_CLOUD_PROJECT: str = ""
    GOOGLE_CLOUD_LOCATION: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def model_post_init(self, __context: object) -> None:
        # Allow .env overrides for local development; only hit Secret Manager when empty
        if not self.TELEGRAM_BOT_TOKEN:
            self.TELEGRAM_BOT_TOKEN = load_secret("TELEGRAM_BOT_TOKEN", self.GCP_PROJECT_ID)
        if not self.NEWSDATA_API_KEY:
            self.NEWSDATA_API_KEY = load_secret("NEWSDATA_API_KEY", self.GCP_PROJECT_ID)

        # Propagate ADK env vars so the ADK client picks them up from os.environ
        if self.GOOGLE_GENAI_USE_VERTEXAI:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = self.GOOGLE_GENAI_USE_VERTEXAI
        if self.GOOGLE_CLOUD_PROJECT:
            os.environ["GOOGLE_CLOUD_PROJECT"] = self.GOOGLE_CLOUD_PROJECT
        if self.GOOGLE_CLOUD_LOCATION:
            os.environ["GOOGLE_CLOUD_LOCATION"] = self.GOOGLE_CLOUD_LOCATION

        if self.APP_ENV not in ("local", "test") and not self.LOG_PSEUDONYM_SALT:
            raise ValueError(
                "LOG_PSEUDONYM_SALT must be set in non-local environments. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()  # type: ignore[call-arg]


settings: Settings = get_settings()
