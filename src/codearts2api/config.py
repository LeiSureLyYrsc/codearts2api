"""Environment-driven settings. Secrets must come from the environment, never from source."""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SNAP_BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
DEFAULT_TIMEZONE = "Asia/Shanghai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    codearts_ak: SecretStr | None = Field(default=None)
    codearts_sk: SecretStr | None = Field(default=None)
    codearts_security_token: SecretStr | None = Field(default=None)
    codearts_project_id: str | None = Field(default=None)

    snap_base_url: str = Field(default=DEFAULT_SNAP_BASE_URL)

    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("CA2A_HOST", "CODEARTS2API_HOST"),
    )
    port: int = Field(
        default=8787,
        validation_alias=AliasChoices("CA2A_PORT", "CODEARTS2API_PORT"),
    )
    api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("CA2A_API_KEY", "CODEARTS2API_API_KEY"),
    )

    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        validation_alias=AliasChoices("CA2A_TIMEZONE", "CODEARTS2API_TIMEZONE"),
    )
    checkin_cron: str = Field(
        default="0 8 * * *",
        validation_alias=AliasChoices("CA2A_CHECKIN_CRON", "CODEARTS2API_CHECKIN_CRON"),
    )
    checkin_run_on_startup: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CA2A_CHECKIN_RUN_ON_STARTUP",
            "CODEARTS2API_CHECKIN_RUN_ON_STARTUP",
        ),
    )
    scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CA2A_SCHEDULER_ENABLED",
            "CODEARTS2API_SCHEDULER_ENABLED",
        ),
    )
    campaign_type: str = Field(
        default="USER_LOGIN",
        validation_alias=AliasChoices("CA2A_CAMPAIGN_TYPE", "CODEARTS2API_CAMPAIGN_TYPE"),
    )
    checkin_claim_all: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CA2A_CHECKIN_CLAIM_ALL",
            "CODEARTS2API_CHECKIN_CLAIM_ALL",
        ),
    )

    model_cache_ttl: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "CA2A_MODEL_CACHE_TTL",
            "CODEARTS2API_MODEL_CACHE_TTL",
        ),
    )

    x_language: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CA2A_X_LANGUAGE", "CODEARTS2API_X_LANGUAGE"),
    )

    opengw_base_url: str | None = Field(
        default="https://opengw.developer.huaweicloud.com",
        validation_alias=AliasChoices("OPENGW_BASE_URL", "CA2A_OPENGW_BASE_URL"),
    )

    auth_dir: str = Field(
        default="auths",
        validation_alias=AliasChoices("CA2A_AUTH_DIR", "CODEARTS2API_AUTH_DIR"),
    )
    credential_source: str = Field(
        default="auto",
        validation_alias=AliasChoices("CA2A_CREDENTIAL_SOURCE", "CODEARTS2API_CREDENTIAL_SOURCE"),
    )
    oauth_client_id: str = Field(
        default="CodeArts_Tui",
        validation_alias=AliasChoices("CA2A_OAUTH_CLIENT_ID", "CODEARTS2API_OAUTH_CLIENT_ID"),
    )
    oauth_portal_url: str = Field(
        default="https://codearts.huaweicloud.com/portal",
        validation_alias=AliasChoices("CA2A_OAUTH_PORTAL_URL", "CODEARTS2API_OAUTH_PORTAL_URL"),
    )
    oauth_sts_url: str = Field(
        default="https://sts.cn-north-4.myhuaweicloud.com",
        validation_alias=AliasChoices("CA2A_OAUTH_STS_URL", "CODEARTS2API_OAUTH_STS_URL"),
    )
    oauth_callback_port: int = Field(
        default=0,
        validation_alias=AliasChoices("CA2A_OAUTH_CALLBACK_PORT", "CODEARTS2API_OAUTH_CALLBACK_PORT"),
    )
    oauth_locale: str = Field(
        default="zh-cn",
        validation_alias=AliasChoices("CA2A_OAUTH_LOCALE", "CODEARTS2API_OAUTH_LOCALE"),
    )

    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("CA2A_LOG_LEVEL", "CODEARTS2API_LOG_LEVEL"),
    )
    http_timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("CA2A_HTTP_TIMEOUT", "CODEARTS2API_HTTP_TIMEOUT"),
    )
    http_stream_timeout: float = Field(
        default=300.0,
        validation_alias=AliasChoices(
            "CA2A_HTTP_STREAM_TIMEOUT",
            "CODEARTS2API_HTTP_STREAM_TIMEOUT",
        ),
    )

    @field_validator("credential_source")
    @classmethod
    def _credential_source(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "oauth", "env"}:
            raise ValueError("CA2A_CREDENTIAL_SOURCE must be auto, oauth, or env")
        return normalized

    @field_validator("oauth_portal_url", "oauth_sts_url")
    @classmethod
    def _strip_oauth_urls(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("snap_base_url")
    @classmethod
    def _strip_snap_base(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("opengw_base_url")
    @classmethod
    def _strip_opengw(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value.strip().rstrip("/")

    @field_validator("x_language", "codearts_project_id")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value.strip()

    def has_credentials(self) -> bool:
        return bool(
            self.codearts_ak
            and self.codearts_ak.get_secret_value()
            and self.codearts_sk
            and self.codearts_sk.get_secret_value()
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.get_secret_value())

    def secret_values_for_redaction(self) -> list[str]:
        values: list[str] = []
        for item in (
            self.codearts_ak,
            self.codearts_sk,
            self.codearts_security_token,
            self.api_key,
        ):
            if item is None:
                continue
            raw = item.get_secret_value()
            if raw:
                values.append(raw)
        return values
