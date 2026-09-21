"""CrawlContext.fetch: retry/backoff decisions, in particular that a bare 429 -
GitLab and Gitea/Codeberg don't echo GitHub's "rate limit" wording - still gets
treated as a rate limit rather than a dead end. See base.py's fetch() for why this
matters especially for the unauthenticated self-hosted GitLab instances."""

import asyncio

import httpx
import pytest
import respx

from flatsonar_server.crawler.base import CrawlContext
from flatsonar_server.settings import settings


@pytest.fixture()
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crawl_cache_dir", tmp_path / "crawl-cache")
    c = CrawlContext(concurrency=2, min_interval=0)
    yield c
    await c.close()


@pytest.fixture()
def no_sleep(monkeypatch):
    """Backoff delays are real seconds (up to 900); tests should not actually wait."""
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


@respx.mock
async def test_bare_429_without_rate_limit_wording_is_still_retried(ctx, no_sleep):
    route = respx.get("https://example.test/x").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "5"}, text="Too Many Requests"),
            httpx.Response(200, text="ok"),
        ]
    )
    r = await ctx.fetch("https://example.test/x", use_cache=False)
    assert r.ok and r.text == "ok"
    assert route.call_count == 2
    assert no_sleep == [5.0]


@respx.mock
async def test_ordinary_403_is_not_treated_as_a_rate_limit(ctx, no_sleep):
    """A private/forbidden repo must not be retried for up to 15 minutes on the
    (false) assumption it is rate limiting."""
    route = respx.get("https://example.test/y").mock(return_value=httpx.Response(403, text="Forbidden"))
    r = await ctx.fetch("https://example.test/y", use_cache=False)
    assert not r.ok and r.status == 403
    assert route.call_count == 1
    assert no_sleep == []


@respx.mock
async def test_403_with_rate_limit_wording_is_still_retried(ctx, no_sleep):
    """GitHub's own phrasing keeps working the way it always did."""
    route = respx.get("https://example.test/z").mock(
        side_effect=[
            httpx.Response(403, text="API rate limit exceeded for x.x.x.x."),
            httpx.Response(200, text="ok"),
        ]
    )
    r = await ctx.fetch("https://example.test/z", use_cache=False)
    assert r.ok and r.text == "ok"
    assert route.call_count == 2
    assert no_sleep == [60.0]  # no reset/retry-after header: falls back to the default
