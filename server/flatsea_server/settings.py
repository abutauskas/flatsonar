from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_SERVER_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite:///{(_SERVER_DIR / 'flatsea.db').as_posix()}"
    github_token: str | None = None
    gitlab_token: str | None = None
    codeberg_token: str | None = None
    crawl_cache_dir: Path = _SERVER_DIR / ".crawl-cache"
    cors_origins: str = "*"
    user_agent: str = "Flatsea/0.1 (+https://github.com/abutauskas/flatsea)"


settings = Settings()
