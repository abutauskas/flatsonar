"""Independent Flatpak remotes: enumerate a .flatpakrepo's summary, cross-reference
against its appstream catalogue, and only list what clears the open-source gate."""

import asyncio
import gzip

import httpx

from flatsonar_server.crawler.base import Fetched
from flatsonar_server.crawler.remotes import RemoteCatalogue, ThirdPartyRemotesSource
from flatsonar_server.models import SourceKind

FLATPAKREPO = """[Flatpak Repo]
Title=Test Remote
Url=https://repo.example/repo
GPGKey=notreallyakey
"""

APPSTREAM = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.8" origin="flatpak">
  <component type="desktop-application">
    <id>org.example.Foo</id>
    <name>Foo</name>
    <summary>An example app</summary>
    <project_license>MIT</project_license>
    <developer_name>Alice</developer_name>
    <url type="homepage">https://example.org/foo</url>
  </component>
  <component type="desktop-application">
    <id>com.example.Proprietary</id>
    <name>Proprietary Thing</name>
    <summary>Not open source</summary>
    <project_license>LicenseRef-proprietary</project_license>
  </component>
</components>
"""


def _off_size(n: int) -> int:
    # Mirrors ostree_summary._offset_size; duplicated here because this is the
    # test-only *encoder* (the inverse operation), not a caller of the decoder.
    if n == 0:
        return 0
    if n <= 0xFF:
        return 1
    if n <= 0xFFFF:
        return 2
    return 4


def _encode_ref_entry(name: str) -> bytes:
    name_bytes = name.encode() + b"\x00"
    name_end = len(name_bytes)
    body = b"\x00" * 8 + b"\x00" * 32
    pad = -name_end % 8
    data = name_bytes + b"\x00" * pad + body
    off = 1  # entries in these fixtures are always well under 256 bytes
    while _off_size(len(data) + off) != off:
        off += 1
    return data + name_end.to_bytes(off, "little")


def _fixed_point_append(data: bytes, values: list[int]) -> bytes:
    """Append one little-endian offset per value, in order, at whatever width
    _off_size would compute for the *final* total length - found by iterating
    since the width itself affects that length."""
    off = 1
    while True:
        total = len(data) + len(values) * off
        if _off_size(total) == off:
            break
        off += 1
    for v in values:
        data += v.to_bytes(off, "little")
    return data


def _encode_summary(names: list[str]) -> bytes:
    array_buf = b""
    ends = []
    for name in names:
        pad = -len(array_buf) % 8
        array_buf += b"\x00" * pad
        array_buf += _encode_ref_entry(name)
        ends.append(len(array_buf))
    array_member = _fixed_point_append(array_buf, ends)  # the array, offset table included
    return _fixed_point_append(array_member, [len(array_member)])  # + the outer tuple's one offset


SUMMARY = _encode_summary([
    "app/org.example.Foo/x86_64/stable",
    "app/com.example.Proprietary/x86_64/stable",
    "runtime/org.freedesktop.Platform/x86_64/23.08",
])


class FakeCtx:
    def __init__(self, texts: dict[str, str] | None = None, blobs: dict[str, bytes] | None = None):
        self.texts = texts or {}
        self.blobs = blobs or {}

    async def fetch(self, url, headers=None, use_cache=True):
        if url in self.texts:
            return Fetched(200, self.texts[url], httpx.Headers())
        return Fetched(404, "", httpx.Headers())

    async def fetch_bytes(self, url):
        return self.blobs.get(url)


def _ctx(appstream_ok: bool = True) -> FakeCtx:
    blobs = {"https://repo.example/repo/summary": SUMMARY}
    if appstream_ok:
        blobs["https://repo.example/repo/appstream/x86_64/appstream.xml.gz"] = gzip.compress(APPSTREAM.encode())
    return FakeCtx(texts={"https://example.org/test.flatpakrepo": FLATPAKREPO}, blobs=blobs)


def test_only_the_open_source_app_is_listed():
    cands = asyncio.run(RemoteCatalogue("https://example.org/test.flatpakrepo").discover(_ctx()))
    assert [c.app_id for c in cands] == ["org.example.Foo"]
    c = cands[0]
    assert c.name == "Foo" and c.license == "MIT" and c.is_oss is True
    assert c.developer_name == "Alice" and c.homepage == "https://example.org/foo"
    assert len(c.sources) == 1
    src = c.sources[0]
    assert src.kind is SourceKind.REMOTE
    assert src.remote_url == "https://example.org/test.flatpakrepo"  # the .flatpakrepo file itself, not Url=
    assert src.remote_name == "test-remote"
    assert src.ref == "app/org.example.Foo/x86_64/stable"


def test_no_appstream_catalogue_yields_nothing():
    cands = asyncio.run(RemoteCatalogue("https://example.org/test.flatpakrepo").discover(_ctx(appstream_ok=False)))
    assert cands == []


def test_unfetchable_flatpakrepo_yields_nothing():
    cands = asyncio.run(RemoteCatalogue("https://example.org/missing.flatpakrepo").discover(_ctx()))
    assert cands == []


def test_no_url_in_flatpakrepo_yields_nothing():
    ctx = FakeCtx(texts={"https://example.org/bad.flatpakrepo": "[Flatpak Repo]\nTitle=No Url Here\n"})
    cands = asyncio.run(RemoteCatalogue("https://example.org/bad.flatpakrepo").discover(ctx))
    assert cands == []


def test_source_iterates_all_remotes_and_survives_one_failing(monkeypatch):
    good_url = "https://example.org/test.flatpakrepo"
    bad_url = "https://example.org/broken.flatpakrepo"
    ctx = _ctx()

    class BoomCatalogue:
        def __init__(self, url):
            self.url = url

        async def discover(self, ctx):
            if self.url == bad_url:
                raise RuntimeError("network exploded")
            return await RemoteCatalogue(self.url).discover(ctx)

    monkeypatch.setattr("flatsonar_server.crawler.remotes.RemoteCatalogue", BoomCatalogue)
    source = ThirdPartyRemotesSource([bad_url, good_url])

    async def collect():
        return [c async for c in source.discover(ctx)]

    cands = asyncio.run(collect())
    assert [c.app_id for c in cands] == ["org.example.Foo"]
