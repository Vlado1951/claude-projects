from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Required
    telegram_token: str
    telegram_chat_id: str

    # Optional with defaults
    truth_social_handle: str = "realDonaldTrump"
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 42
    news_poll_interval_seconds: int = 30
    llm_provider: Literal["anthropic", "openai", "none"] = "none"
    llm_api_key: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def check_llm_key(self) -> "Config":
        if self.llm_provider != "none" and not self.llm_api_key:
            raise ValueError(
                f"LLM_API_KEY is required when LLM_PROVIDER='{self.llm_provider}'"
            )
        return self
