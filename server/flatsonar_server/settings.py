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
    # Self-hosted GitLab instances to hunt besides gitlab.com, comma-separated. Where a lot
    # of desktop-Linux software actually lives: GNOME, KDE and freedesktop.org each run their
    # own instance and gitlab.com search alone never sees them.
    gitlab_instances: str = (
        "https://gitlab.gnome.org,https://invent.kde.org,"
        "https://gitlab.freedesktop.org,https://gitlab.xfce.org"
    )
    crawl_cache_dir: Path = _SERVER_DIR / ".crawl-cache"
    cors_origins: str = "*"
    site_url: str | None = None  # public origin for sitemap/feed links, e.g. https://flatsonar.org
    user_agent: str = "Flatsonar/0.1 (+https://github.com/abutauskas/flatsonar)"
    # Gates /admin/analytics. Unset (the default) disables the route entirely -
    # it 404s rather than falling open. Set to a long random value to enable it.
    admin_token: str | None = None


settings = Settings()
