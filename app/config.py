"""Application settings, loaded from environment variables (and `.env` in development).

No secret has a default value: anything sensitive must come from the environment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]

DEFAULT_TITLES = ["Chief", "VP", "Vice President", "Head of", "Director", "Founder"]
DEFAULT_TITLE_EXCLUDES = ["Intern", "Assistant", "Associate Director", "Former"]


class Settings(BaseSettings):
    # env_ignore_empty: `FORAGER_ACCOUNT_ID=` (blank) means "unset", not a parse error.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # --- General -------------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Forager ---------------------------------------------------------------------
    forager_api_key: SecretStr | None = None
    forager_account_id: int | None = None
    forager_base_url: str = "https://api-v2.forager.ai"
    forager_timeout_seconds: float = 30.0
    forager_cache_enabled: bool = True
    forager_cache_dir: Path = Path(".cache/forager")
    # Hard ceiling on credits a single CLI run may spend. 0 = no paid calls at all.
    forager_max_credits_per_run: int = Field(default=0, ge=0)
    # Refuse to spend if the account balance would drop below this.
    forager_min_balance_reserve: int = Field(default=0, ge=0)
    forager_work_email_poll_timeout_seconds: float = 180.0

    # --- HubSpot ---------------------------------------------------------------------
    hubspot_access_token: SecretStr | None = None
    hubspot_client_secret: SecretStr | None = None
    hubspot_base_url: str = "https://api.hubapi.com"
    # HubSpot's date-versioned REST APIs (replace /crm/v3, /crm/v4 for new integrations).
    hubspot_api_version: str = "2026-09"
    hubspot_timeout_seconds: float = 30.0
    # Public base URL HubSpot calls (used to rebuild the signed URI behind proxies).
    public_base_url: str | None = None
    webhook_max_timestamp_skew_seconds: int = 300

    # --- Contact discovery -------------------------------------------------------------
    contact_titles: CsvList = Field(default_factory=lambda: list(DEFAULT_TITLES))
    contact_title_excludes: CsvList = Field(default_factory=lambda: list(DEFAULT_TITLE_EXCLUDES))

    # --- Webhook-driven enrichment (cost controls) ------------------------------------
    webhook_enrichment_enabled: bool = True
    webhook_max_credits_per_event: int = Field(default=25, ge=0)
    webhook_max_new_contacts: int = Field(default=2, ge=0)
    webhook_lookup_work_email: bool = True
    webhook_lookup_personal_email: bool = False
    webhook_lookup_phone: bool = False
    webhook_worker_concurrency: int = Field(default=1, ge=1, le=8)
    # Contacts stuck "in_progress" longer than this are considered abandoned and retried.
    enrichment_stale_after_minutes: int = Field(default=30, ge=1)

    @field_validator("contact_titles", "contact_title_excludes", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("forager_base_url", "hubspot_base_url", "public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    # Convenience checks used by the CLI / app to fail fast with a clear message.
    def require_forager(self) -> tuple[str, int]:
        if not self.forager_api_key or not self.forager_account_id:
            raise ConfigError("FORAGER_API_KEY and FORAGER_ACCOUNT_ID must be set.")
        return self.forager_api_key.get_secret_value(), self.forager_account_id

    def require_hubspot(self) -> str:
        if not self.hubspot_access_token:
            raise ConfigError("HUBSPOT_ACCESS_TOKEN must be set.")
        return self.hubspot_access_token.get_secret_value()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
