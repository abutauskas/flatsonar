from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_SERVER_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite:///{(_SERVER_DIR / 'flatsonar.db').as_posix()}"
    github_token: str | None = None
    gitlab_token: str | None = None
    codeberg_token: str | None = None
    crawl_cache_dir: Path = _SERVER_DIR / ".crawl-cache"
    cors_origins: str = "*"
    site_url: str | None = None  # public origin for sitemap/feed links, e.g. https://flatsonar.org
    user_agent: str = "Flatsonar/0.1 (+https://github.com/abutauskas/flatsonar)"


settings = Settings()
