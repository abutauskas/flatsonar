# Flatsonar

**F-Droid for Linux.** Flatsonar hunts down open-source Flatpak apps wherever they
live — Flathub, GitHub, GitLab, Codeberg, project-hosted remotes, `.flatpak`
bundles attached to releases — and puts them in one store. Every listing
credits the original creators and shows a **Sponsor** button.

You can install anything. Flatsonar reads each app's sandbox permissions and runs
a local ClamAV scan before deploying; if something looks dangerous it warns you
and asks you to press **Sure** twice. After that, it's your call.

## Layout

| Directory | What | Runs on |
|-----------|------|---------|
| `core/`   | `flatsonar_core` — manifest parsing, risk scoring, SPDX allow-list | anywhere |
| `server/` | `flatsonar_server` — FastAPI + crawler ("the hunt") | anywhere |
| `client/` | `flatsonar` — GTK4 / libadwaita desktop app | Linux (WSL2 + WSLg is fine for dev) |

## Quick start

### Server (Windows or Linux)

```sh
python -m venv .venv
. .venv/Scripts/activate        # Linux: . .venv/bin/activate
pip install -e core -e server
cp server/.env.example server/.env   # add GITHUB_TOKEN for off-Flathub hunting
python -m flatsonar_server.crawler.run --source flathub --limit 100
uvicorn flatsonar_server.main:app --reload
```

Then open http://localhost:8000/docs.

### Client (Linux / WSL2)

```sh
./scripts/setup-wsl.sh          # once: GTK4, libadwaita, flatpak, clamav
pip install --user -e core -e client
FLATSONAR_API=http://localhost:8000 python -m flatsonar
```

On Windows the server can stay on the Windows side; WSL2 reaches it at
`http://localhost:8000` automatically. Flatpak works inside WSL2 for development
(GUI apps show up through WSLg), but test on a real Linux install before trusting
anything sandbox-related.

### Tests

```sh
pytest core/tests server/tests client/tests
```

The client's install pipeline (pull -> inspect -> ClamAV -> warn twice -> deploy) is
tested with fakes, so the whole suite runs on Windows without GTK or flatpak.

## How the warning works

1. `flatpak install --no-deploy` pulls the app into the local OSTree repo without deploying it.
2. `ostree checkout` materialises the files; ClamAV scans them.
3. The deployed `metadata` file is parsed back into `finish-args` and scored with
   `flatsonar_core.risk` (host filesystem, `--device=all`, session/system bus, sandbox
   escape via `org.freedesktop.Flatpak`, `LD_PRELOAD`, credential paths ... -> red;
   X11, home folder, keyring, ... -> yellow). The index's score is only a preview; the
   real files decide.
4. Yellow or red: dialog one lists the findings, dialog two says it's on you.
   Two "Sure"s and it deploys. Accepted findings are remembered per app until
   its permissions change.

Flatsonar packaged as a Flatpak scores **red** by its own rules: a store has to talk to
`org.freedesktop.Flatpak` to install things on the host. That is the honest answer.

## Hunting

`python -m flatsonar_server.crawler.run --source all` walks:

- **Flathub** - API v2 for metadata and the *deployed* permissions, plus the
  `github.com/flathub/<id>` manifest for `sources` (that's where the upstream repo and
  creator credit come from).
- **GitHub / GitLab / Codeberg** - repos with the `flatpak` topic (and GitHub code search
  with a token) whose tree contains a `reverse.dns.Name.{json,yml,yaml}` manifest.
  `.metainfo.xml` gives name/summary/license/screenshots, `FUNDING.yml` and AppStream
  `<url type="donation">` give the sponsor buttons, release assets ending in `.flatpak`
  become one-click bundle installs, `.flatpakrepo` files become remotes, and anything
  with only a manifest gets built locally with `flatpak-builder`.

Only OSI/FSF-approved SPDX licenses are listed. Proprietary apps on Flathub are skipped.

## License

GPL-3.0-or-later. Every app listed in Flatsonar belongs to its own authors under
its own license — Flatsonar just points at them.
