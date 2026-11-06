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

    # GitHub Models (legacy / dev only — disabled for data-sovereign demos)
    github_token: str = ""
    model_id: str = "qwen3-vl-235b-awq"
    # Optional override to point the VLM layer at a self-hosted server
    # (vLLM / SGLang / anything OpenAI-compatible). Empty -> GitHub Models.
    vlm_base_url: str = ""
    # API key for the override endpoint. Empty -> falls back to GITHUB_TOKEN.
    vlm_api_key: str = ""
    # Safety rail: cloud-hosted models are hidden and cannot be selected unless
    # this is explicitly enabled. Keep false for the Cisco/Curtin expo so no
    # frame can ever leave the local inference path by accident.
    allow_cloud_models: bool = False

    # Demo profile: when set, load config/cameras.<profile>.yaml instead of
    # config/cameras.yaml. Use DEMO_PROFILE=replay to switch the whole app to
    # recorded footage without editing the live camera config.
    demo_profile: str = ""

    # App
    api_key: str = "changeme"
    secret_key: str = "changeme"
    debug: bool = False
    # Decoupled from DEBUG so you can run real data with verbose logging.
    # When true, app/agents/demo.py fires synthetic incidents every 15s.
    demo_agent: bool = False
    # Start the video-ingest tasks for cameras that declare a ``source`` block
    # in config/cameras.yaml. Off by default so existing Meraki-only installs
    # are unaffected.
    ingest_enabled: bool = False

    # Storage
    data_dir: Path = Path("data")
    site_name: str = "Innovation Central Perth"
    # Product name shown in the dashboard header/title and API docs.
    product_name: str = "Safety Sentry"
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
