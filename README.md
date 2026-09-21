# Flatsonar

**Hunted, not submitted.** Flatsonar hunts down open-source Flatpak apps wherever they
live (Flathub, GitHub, GitLab, Codeberg, project-hosted remotes, `.flatpak`
bundles attached to releases) and puts them in one store. Every listing
credits the original creators and shows a **Sponsor** button.

You can install anything, from anyone: verified developers, community packagers,
and people nobody has heard of. Flatsonar never hides an app for being unverified.
What it does instead is work out **who is publishing it**, **what its build does**,
and **what its sandbox allows**, and tell you before anything runs. If something
looks off it asks you to press **Sure** twice. After that, it's your call.

## Get it

Browse the catalogue at **[flatsonar.org](https://flatsonar.org)**, or install the desktop
client from the [latest release](https://github.com/abutauskas/flatsonar/releases/latest):

```sh
flatpak install flatsonar.flatpak
```

It talks to the hosted API at `https://flatsonar.onrender.com` by default - no server setup
needed. (That server is on a free instance that spins down when idle, so the first request
after a quiet spell can take up to a minute.)

## Layout

| Directory | What | Runs on |
|-----------|------|---------|
| `core/`   | `flatsonar_core`: manifest parsing, risk scoring, SPDX allow-list | anywhere |
| `server/` | `flatsonar_server`: FastAPI JSON API, the website, and the crawler ("the hunt") | anywhere |
| `client/` | `flatsonar`: GTK4 / libadwaita desktop app | Linux (WSL2 + WSLg is fine for dev) |

## Quick start

### Server (Windows or Linux)

```sh
python -m venv .venv
. .venv/Scripts/activate        # Linux: . .venv/bin/activate
pip install -e core -e server
echo "GITHUB_TOKEN=ghp_..." > server/.env   # off-Flathub hunting needs it; see below for the rest
python -m flatsonar_server.crawler.run --source flathub --limit 100
uvicorn flatsonar_server.main:app --reload
```

Then open http://localhost:8000 for the website (catalogue, app pages, about) or
http://localhost:8000/docs for the API.

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

### Previewing the client on Windows

There is no flatpak on Windows, but the GTK UI runs under MSYS2 and the store can be
driven end to end against a **simulated flatpak**:

```powershell
.\scripts\preview-windows.ps1          # starts the server if needed, launches the client
.\scripts\preview-windows.ps1 -Reset   # back to the seeded scenario
```

`FLATSONAR_FAKE_FLATPAK=1` swaps `flatpak_cli` for `flatsonar.install.fake_flatpak`:
installs, updates and removals stage a commit, hand the pipeline a real `metadata`
file to check out and score, and "deploy" it, so every dialog and gate is the real one.
The seeded scenario (Ctrl+I) has an update that changes nothing (Jan: no dialog), one
that adds `--filesystem=host` (Cantara: two warnings), a red app (Ptyxis), a local
build the index has a newer version of (Stockpile: rebuilds from its manifest) and an
app the index has never seen. State lives in `~/.cache/flatsonar/fake-flatpak.json`;
edit it for other scenarios. Needs `pacman -S mingw-w64-ucrt-x86_64-{python-gobject,libadwaita,python-httpx,python-yaml}`.

### Tests

```sh
pytest core/tests server/tests client/tests
```

The client's install pipeline (pull -> inspect -> ClamAV -> warn twice -> deploy) is
tested with fakes, so the whole suite runs on Windows without GTK or flatpak.

## Who publishes this? (trust)

An app id is a claim: `io.github.alice.Foo` says "alice on GitHub made this",
`org.mozilla.firefox` says "Mozilla made this". Flathub checks that claim by hand;
Flatsonar re-derives the rules in `flatsonar_core.provenance` and applies them to
wherever the manifest was actually found. Every app gets one of four levels:

| Level | Meaning |
|-------|---------|
| **verified** | The creator demonstrably controls the id: Flathub verification, or the hosting account owns the namespace (`io.github.alice.*` hosted by alice, `org.gnome.*` on gitlab.gnome.org, ...), or the id's domain serves `/.well-known/org.flathub.VerifiedApps.txt` listing it. |
| **reviewed** | On Flathub: manifest reviewed and built by Flathub, but the developer has not verified the id. |
| **unverified** | Off-Flathub and ownership could not be confirmed (a custom domain with no well-known file, or a third-party packager building someone else's code). Nothing known against it. |
| **suspicious** | A concrete red flag: the id claims a namespace the repo neither owns nor builds from (`org.mozilla.*` from a random account), or the build does things a build should not (see below), or a `.flatpak` bundle served over plain HTTP. |

Yellow notes never downgrade an app on their own (a repo that is three days old
with no stars, a name shared with a Flathub app, an id someone else also
publishes), but the client lists them before installing.

The crawler also guards the id itself. One app id, one publisher: Flathub always
keeps its row, and a repo claiming a Flathub app's id may only enrich it when it
*is* the repo Flathub builds from (so nobody can plant a sponsor button on someone
else's app). Between two off-Flathub publishers the first one seen keeps the id
unless the newcomer is strictly more trusted, i.e. the real owner turning up after
a copy. Losers are logged and counted as skipped.

`flatsonar_core.audit` reads what the manifest *does* before anything is built:
`extra-data` (downloaded at install time, after any scan), sources over plain
`http://`, archives without a checksum and git sources tracking a branch, prebuilt
binaries instead of source, `build-args` that open the build sandbox
(`--share=network`, `--filesystem=host`, `--talk-name=org.freedesktop.Flatpak`),
and build commands such as `curl | sh`, `base64 -d`, setuid bits or
`flatpak-spawn --host`. The API exposes all of this as `trust` and
`trust_findings`; `/api/apps?trust=verified,reviewed` filters on it.

## Is it still maintained?

Flathub's curation implicitly vouches that a listing works: somebody submitted and
built it recently enough to pass review. Flatsonar indexes straight from source
control, so a listing might be a thriving project or a five-year-old fork nobody
came back to, and nothing said which. `flatsonar_core.assess_maintenance` turns the
repository signal the crawler already collected (archived flag, last push date)
into one of three levels, exposed as `maintenance` and `maintenance_findings`:

| Level | Meaning |
|-------|---------|
| **active** | Recent activity upstream, or on Flathub, where review and automatic updates are already a maintenance signal - every Flathub app is `active` here regardless of its own repository's pace. |
| **stale** | No commits in a year or more. Might still work fine; nobody has had a reason to touch it. |
| **abandoned** | The upstream repository is archived, or has had no commits in several years. |

Like trust, this is a label, not a filter or a risk score: an abandoned app is
never removed, hidden, or scored as dangerous, and `/api/apps?maintenance=stale,abandoned`
only narrows what a search *shows*. A five-year-old utility that never needed
another commit is not a security problem, so this never feeds the sandbox-risk
score or the two-gate install warning below - it is a note about the people
behind an app, not the permissions it asks for.

A separate, narrower signal tracks the build itself: `App.manifest_ok` records
whether the crawler's *most recent* visit could still parse the manifest that got
an app listed in the first place. A YAML typo, a renamed file, a source moved out
from under it - none of that deletes the listing (the last good data keeps
serving), but the app page says plainly that the build may be broken, with the
parse error attached, rather than silently showing stale information as if it
were current. Applying it is guarded the same way id collisions are: it only ever
marks an *already-indexed* app from the *same* repository, so an unrelated file
that happens to share a filename can never mark someone else's app broken.

## How the warning works

The install flow has two gates, because two different things can be wrong.

**Gate one, before anything runs.** The publisher findings above, plus, for apps
built locally from a manifest, a fresh audit of the manifest that was just
downloaded (never the index's copy; the manifest must also build the app id it
claims). For manifest installs this gate comes *before* `flatpak-builder` starts:
the build is the risk. Installing from a project's own remote also warns that
future updates will come from that remote.

**Gate two, after pull or build.**

1. `flatpak install --no-deploy` pulls the app into the local OSTree repo without deploying it
   (or `flatpak-builder` builds it into a local repo).
2. `ostree checkout` materialises the files; ClamAV scans them.
3. The deployed `metadata` file is parsed back into `finish-args` and scored with
   `flatsonar_core.risk` (host filesystem, `--device=all`, session/system bus, sandbox
   escape via `org.freedesktop.Flatpak`, `LD_PRELOAD`, credential paths ... -> red;
   X11, home folder, keyring, ... -> yellow). The index's score is only a preview; the
   real files decide.
4. Yellow or red: dialog one lists the findings, dialog two says it's on you.
   Two "Sure"s and it deploys. Gate two is only shown if it adds findings gate one
   did not already list. Accepted findings are remembered per app until they change.

An unverified publisher alone is yellow: a perfectly sandboxed app from someone
nobody has vouched for still gets the two dialogs. A suspicious one is red.

Flatsonar packaged as a Flatpak scores **red** by its own rules: a store has to talk to
`org.freedesktop.Flatpak` to install things on the host. That is the honest answer.

## Installed apps and updates

**Installed** (Ctrl+I) lists every Flatpak app on the machine, user and system
installations alike, whether or not Flatsonar installed it, and scores each one from
its deployed `metadata` with the same rules as the catalogue. Apps the index knows
open their store page; the rest are still listed and scored, because auditing what is
already there is the point.

Updates go through gate two again. For apps from a remote (Flathub, a project remote)
`flatpak update --no-deploy` pulls the new commit; Flatsonar checks it out, scans and
re-scores it, and only then deploys. You are asked only when the update adds something
you have not already accepted: a new permission, or a publisher the index has since
flagged. Apps Flatsonar built from a manifest, or installed from a bundle, have no
remote to update from; when the index lists a newer version than the one installed,
**Update** rebuilds or re-downloads through the normal install flow.

## The website

The server also serves the store as a website, rendered from the same catalogue
queries as `/api` (`server/flatsonar_server/web/`, Jinja2 templates and one hand-written
stylesheet, no build step):

| Path | What |
|------|------|
| `/` | Landing page: what Flatsonar is, catalogue numbers, trust levels, newest and off-Flathub finds |
| `/apps` | The catalogue: search, category sidebar, risk / trust / where filters, sort, pagination |
| `/apps/<id>` | One app: sponsor buttons, screenshots, description, every permission with its level, publisher findings, install commands per source, details |
| `/about` | The hunt, the trust model, the two gates, getting listed, the API |
| `/sitemap.xml`, `/feed.xml`, `/robots.txt` | For crawlers and feed readers |

Light and dark follow the system. Everything works without JavaScript; the small
script adds copy buttons and auto-applying filters. Set `SITE_URL` in `server/.env`
so the sitemap and feed carry the public origin.

Deploy with the `Dockerfile` at the repository root (API + website + crawler in one
image, listens on `$PORT`, keeps the database on a `/data` volume):

```sh
docker build -t flatsonar .
docker run -p 8000:8000 -v flatsonar-data:/data flatsonar
docker run --rm -v flatsonar-data:/data flatsonar python -m flatsonar_server.crawler.run --source all
```

### GitHub Pages

GitHub Pages only serves static files, so there is a second, static-only build of
the same site: `python -m flatsonar_server.web.build --out public --site-url
https://<user>.github.io/<repo>` (`server/flatsonar_server/web/build.py`) renders
every page once with the live app's own templates and writes them to disk. The one
thing a static host cannot do is run the `/apps` search/filter query server-side,
so that page ships every open-source app in the HTML (search engines and no-JS
visitors see the whole catalogue) and `site.js` filters, sorts and paginates over
it client-side instead: same look, same URLs (`?category=`, `?risk=`, ...), no
server. `/api` has no static equivalent; `/apps.json` is a flat dump of the same
summary fields as a fallback, linked from the footer.

`.github/workflows/pages.yml` does this automatically: it crawls (see "Keeping it
fresh" below), builds, and deploys to `https://<user>.github.io/<repo>/` (or a
custom domain set in the `SITE_URL` repository variable) via GitHub's official
Pages Actions. It needs **Settings → Pages → Source → GitHub Actions** enabled
once, plus a `DATABASE_URL` repo secret pointing at the same Postgres database
the live server uses (a Supabase connection-pooler URL, not the direct one:
GitHub's runners have no IPv6, and the direct URL is often IPv6-only). That
database, not the repo, is what keeps `first_seen` dates, the "new in the
catalogue" section and the Atom feed meaningful between runs.

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
- **Independent Flatpak remotes** (`--source remotes`) - distros, projects and
  organisations that publish their own browsable catalogue instead of building
  through Flathub (GNOME's nightly builds, elementary's AppCenter, ...) never had a
  manifest sitting in a repo for the crawler above to find. `settings.third_party_remotes`
  is a seed list of their `.flatpakrepo` URLs - there is no search API for "find
  Flatpak remotes on the internet", so unlike everything else here, these have to be
  known in advance. For each, `flatsonar_core.ostree_summary` reads `<url>/summary`
  (the ref list every OSTree remote serves over plain HTTP, no client or auth needed)
  and cross-references it against `<url>/appstream/<arch>/appstream.xml.gz` when the
  remote exports one; only an app in both, with an open-source license, gets listed -
  a remote's ref list alone carries no license information. A remote that doesn't
  export that appstream file is enumerated but not yet listed from (reading it needs
  walking OSTree's commit/tree/file objects directly, which isn't implemented).

**Opting out.** Tag a repository with the topic `noflatsonar` (or `no-flatsonar`) on
GitHub, GitLab or Codeberg and the crawler skips it entirely - it never reads the
manifest, and if the repo was already listed from before the topic was added, the
next crawl removes that listing. Checked before the tree is even fetched, straight
off the repo metadata every search already returns.

Only OSI/FSF-approved SPDX licenses are listed. Proprietary apps on Flathub are skipped.
Verification is never a filter for the crawler: unverified publishers are indexed
like everyone else and labelled honestly.

GitLab hunting is not limited to gitlab.com: `settings.gitlab_instances` (env
`GITLAB_INSTANCES`) also walks gitlab.gnome.org, invent.kde.org,
gitlab.freedesktop.org and gitlab.xfce.org by default, since that is where a
meaningful share of desktop Linux software actually lives. Self-hosted instances
never show up in a gitlab.com-only search.

**No SourceHut.** Also not an oversight: sr.ht's GraphQL API is lookup-by-known-name
only - there is no site-wide search or repo-listing endpoint, by explicit minimalist
design, so there is nothing to "hunt" against. (SourceHut repos still turn up
indirectly and get properly credited when a manifest found elsewhere - on GitHub,
GitLab, or Codeberg - points its `sources` at one; that's unrelated and unaffected.)

**No Bitbucket.** Not an oversight: Bitbucket Cloud removed cross-workspace
repository search entirely on April 14, 2026, and its one remaining
search-adjacent endpoint (`workspaces/{workspace}/search/code`) is
workspace-scoped only - it can't discover repos, only search within a workspace
whose name you already know - and Atlassian has already announced it will be
deprecated on November 1, 2026, on top of requiring OAuth (app passwords were
retired in July 2026). There is currently no way to "hunt" Bitbucket the way
Flatsonar hunts everywhere else; a crawler limited to a hardcoded list of known
workspace names would not actually be hunting anything, so it is not built.

A crawl commit is resilient by design: one manifest that fails to *commit* (as
opposed to one that fails to even parse, which was already handled per-candidate)
no longer takes the rest of a multi-thousand-app run down with it, and one source
failing outright does not stop `--source all` from trying the others. Errors are
recorded on the `CrawlRun` row (`/api/stats` → `last_crawls[].errors`) instead of
being silent or fatal.

### Keeping it fresh

`--limit` exists for a quick local smoke test; a real catalogue wants a full,
unlimited, scheduled crawl. `.github/workflows/pages.yml` runs one weekly
(`workflow_dispatch` for an on-demand run, with an optional `limit` input) using
whatever GitHub Actions injects automatically as `GITHUB_TOKEN` (enough to hunt
GitHub without any setup). For higher rate limits, or to hunt GitLab/Codeberg with
a token, add repo secrets `CRAWLER_GITHUB_TOKEN`, `GITLAB_TOKEN`, `CODEBERG_TOKEN`
(Settings → Secrets and variables → Actions); none need any scope beyond reading
public data. Locally the same tokens go in `server/.env` (see the full list of
settings, and their defaults, in `server/flatsonar_server/settings.py`).

## License

GPL-3.0-or-later. Every app listed in Flatsonar belongs to its own authors under
its own license. Flatsonar just points at them.
