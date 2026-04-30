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

    # ADK backend selection — set to "1" locally to use Vertex AI instead of Gemini API
    GOOGLE_GENAI_USE_VERTEXAI: str = ""
    GOOGLE_CLOUD_PROJECT: str = ""
    GOOGLE_CLOUD_LOCATION: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def model_post_init(self, __context: object) -> None:
        # Allow .env overrides for local development; only hit Secret Manager when empty
        if not self.TELEGRAM_BOT_TOKEN:
            self.TELEGRAM_BOT_TOKEN = load_secret("TELEGRAM_BOT_TOKEN", self.GCP_PROJECT_ID)

        # Propagate ADK env vars so the ADK client picks them up from os.environ
        if self.GOOGLE_GENAI_USE_VERTEXAI:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = self.GOOGLE_GENAI_USE_VERTEXAI
        if self.GOOGLE_CLOUD_PROJECT:
            os.environ["GOOGLE_CLOUD_PROJECT"] = self.GOOGLE_CLOUD_PROJECT
        if self.GOOGLE_CLOUD_LOCATION:
            os.environ["GOOGLE_CLOUD_LOCATION"] = self.GOOGLE_CLOUD_LOCATION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()  # type: ignore[call-arg]


settings: Settings = get_settings()
