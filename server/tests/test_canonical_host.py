"""CanonicalHostMiddleware: pages reached on a non-canonical host (e.g. the Render-assigned
onrender.com address in front of the flatsonar.org custom domain) 301 to SITE_URL's host,
except the paths that must always answer locally: /health (Render's own health checks) and
/api (the desktop client talks to onrender.com directly, see client/flatsonar/api.py)."""

from flatsonar_server import main


def test_redirects_pages_to_the_canonical_host(client, monkeypatch):
    monkeypatch.setattr(main, "_CANONICAL_NETLOC", "flatsonar.org")
    monkeypatch.setattr(main, "_CANONICAL_SCHEME", "https")
    r = client.get("/apps?category=Games", headers={"host": "flatsonar.onrender.com"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://flatsonar.org/apps?category=Games"


def test_never_redirects_health_or_api(client, monkeypatch):
    monkeypatch.setattr(main, "_CANONICAL_NETLOC", "flatsonar.org")
    monkeypatch.setattr(main, "_CANONICAL_SCHEME", "https")
    assert client.get("/health", headers={"host": "flatsonar.onrender.com"}).status_code == 200
    assert client.get("/api/apps", headers={"host": "flatsonar.onrender.com"}).status_code == 200


def test_noop_when_site_url_is_unconfigured(client):
    assert main._CANONICAL_NETLOC is None  # no SITE_URL in the test environment
    r = client.get("/apps", headers={"host": "flatsonar.onrender.com"}, follow_redirects=False)
    assert r.status_code == 200


def test_already_on_the_canonical_host_is_untouched(client, monkeypatch):
    monkeypatch.setattr(main, "_CANONICAL_NETLOC", "testserver")
    monkeypatch.setattr(main, "_CANONICAL_SCHEME", "https")
    assert client.get("/apps").status_code == 200
