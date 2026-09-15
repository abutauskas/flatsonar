# Flatsea

**F-Droid for Linux.** Flatsea hunts down open-source Flatpak apps wherever they
live — Flathub, GitHub, GitLab, Codeberg, project-hosted remotes, `.flatpak`
bundles attached to releases — and puts them in one store. Every listing
credits the original creators and shows a **Sponsor** button.

You can install anything. Flatsea reads each app's sandbox permissions and runs
a local ClamAV scan before deploying; if something looks dangerous it warns you
and asks you to press **Sure** twice. After that, it's your call.

## Layout

| Directory | What | Runs on |
|-----------|------|---------|
| `core/`   | `flatsea_core` — manifest parsing, risk scoring, SPDX allow-list | anywhere |
| `server/` | `flatsea_server` — FastAPI + crawler ("the hunt") | anywhere |
| `client/` | `flatsea` — GTK4 / libadwaita desktop app | Linux (WSL2 + WSLg is fine for dev) |

## Quick start

### Server (Windows or Linux)

```sh
python -m venv .venv
. .venv/Scripts/activate        # Linux: . .venv/bin/activate
pip install -e core -e server
cp server/.env.example server/.env   # add GITHUB_TOKEN for off-Flathub hunting
python -m flatsea_server.crawler.run --source flathub --limit 100
uvicorn flatsea_server.main:app --reload
```

Then open http://localhost:8000/docs.

### Client (Linux / WSL2)

```sh
./scripts/setup-wsl.sh          # once: GTK4, libadwaita, flatpak, clamav
pip install --user -e core -e client
FLATSEA_API=http://localhost:8000 python -m flatsea
```

On Windows the server can stay on the Windows side; WSL2 reaches it at
`http://localhost:8000` automatically.

## License

GPL-3.0-or-later. Every app listed in Flatsea belongs to its own authors under
its own license — Flatsea just points at them.
