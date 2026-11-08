"""Environment-based application configuration."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings are loaded from environment variables and the local .env file."""

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://localhost/isocode_tours"
    ai_enabled: bool = False
    ai_provider: Literal["gemini", "groq"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    ai_timeout_seconds: float = 20.0
    ai_max_retries: int = 1
    ai_fallback_enabled: bool = True

    @field_validator("gemini_api_key", "groq_api_key", mode="before")
    @classmethod
    def blank_keys_are_missing(cls, value: object) -> object:
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator("gemini_model", "groq_model", mode="before")
    @classmethod
    def blank_models_are_missing(cls, value: object) -> object:
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator("ai_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if not 1 <= value <= 60:
            raise ValueError("AI_TIMEOUT_SECONDS must be between 1 and 60.")
        return value

    @field_validator("ai_max_retries")
    @classmethod
    def validate_retry_count(cls, value: int) -> int:
        if not 0 <= value <= 2:
            raise ValueError("AI_MAX_RETRIES must be between 0 and 2.")
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object for the application process."""
    return Settings()


settings = get_settings()
