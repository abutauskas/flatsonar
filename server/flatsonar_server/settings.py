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
    # Other Gitea/Forgejo instances to hunt besides codeberg.org, comma-separated. No
    # defaults here (unlike gitlab_instances): research found no other Gitea/Forgejo
    # instance hosting a meaningful concentration of desktop-Linux apps. This just
    # makes the capability available for whoever finds one worth adding later.
    codeberg_instances: str = ""
    # .flatpakrepo URLs of independent, non-Flathub Flatpak remotes to hunt for their own
    # app catalogues, comma-separated. Unlike the forges above, there is no search API for
    # "find Flatpak remotes on the internet" (see crawler/remotes.py) - these have to be
    # known in advance. Starter list, each verified reachable and enumerable: GNOME's
    # nightly builds, elementary's AppCenter, Dolphin Emulator's own repo, PureOS.
    third_party_remotes: str = (
        "https://nightly.gnome.org/gnome-nightly.flatpakrepo,"
        "https://flatpak.elementary.io/repo.flatpakrepo,"
        "https://flatpak.dolphin-emu.org/releases.flatpakrepo,"
        "https://store.puri.sm/repo/stable/pureos.flatpakrepo"
    )
    crawl_cache_dir: Path = _SERVER_DIR / ".crawl-cache"
    icon_cache_dir: Path = _SERVER_DIR / ".icon-cache"
    cors_origins: str = "*"
    site_url: str | None = None  # public origin for sitemap/feed links, e.g. https://flatsonar.org
    user_agent: str = "Flatsonar/0.1 (+https://github.com/abutauskas/flatsonar)"
    # Gates /admin/analytics. Unset (the default) disables the route entirely -
    # it 404s rather than falling open. Set to a long random value to enable it.
    admin_token: str | None = None


settings = Settings()
