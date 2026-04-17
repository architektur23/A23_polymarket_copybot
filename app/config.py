"""
Central configuration loaded from environment variables / .env file.
Only contains infrastructure settings (URLs, paths, log level).
Polymarket credentials are stored in the database and entered via the UI.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket API base URLs ──────────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"

    # ── Chain ────────────────────────────────────────────────────────────────
    polygon_rpc: str = "https://polygon-rpc.com"
    chain_id: int = 137

    # ── App ──────────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    log_dir: str = "data/logs"
    log_level: str = "INFO"
    # Number of log lines to keep in memory for the UI viewer
    log_buffer_lines: int = 500

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
