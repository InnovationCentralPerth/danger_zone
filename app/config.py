from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Meraki
    meraki_api_key: str = ""
    meraki_org_id: str = ""
    meraki_webhook_secret: str = ""
    # Safety: refuse to start if Meraki key has write scope.
    # Set to true ONLY for local dev where you can't easily create a read-only admin.
    meraki_allow_write_key: bool = False

    # GitHub Models
    github_token: str = ""
    model_id: str = "openai/gpt-4o-mini"

    # App
    api_key: str = "changeme"
    secret_key: str = "changeme"
    debug: bool = False

    # Storage
    data_dir: Path = Path("data")
    snapshot_max_age_days: int = 30

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
