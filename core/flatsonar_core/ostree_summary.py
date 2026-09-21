"""Read the one thing Flatsonar needs from an OSTree repository: its ``summary``
file, which every Flatpak remote serves at ``<url>/summary`` over plain HTTP with no
authentication required. It lists every ref the remote hosts, with its current
commit checksum - the same data ``flatpak remote-ls`` reads - which is how an
arbitrary third-party remote's app catalogue can be enumerated without speaking the
OSTree pull protocol or running libostree.

This is not a general GVariant decoder. It only understands the one type string
OSTree defines for this file (``ostree-core.h``'s ``OSTREE_SUMMARY_GVARIANT_STRING``,
verified against the upstream source)::

    (a(s(taya{sv}))a{sv})

...and only extracts the two things Flatsonar uses (ref name, commit checksum),
skipping the per-ref and per-summary metadata dictionaries entirely rather than
decoding them, since nothing here reads their contents. The byte-level offset
arithmetic below follows the GVariant specification
(developer.gnome.org/documentation/specifications/gvariant-specification-1.0.html,
sections on framing offsets and fixed- vs non-fixed-size types) and has been
verified against a real remote (GNOME Nightly's ~450KB summary): every checksum this
module extracted for the ``appstream2/*`` refs matched the value independently
fetched from that remote's ``refs/heads/appstream2/<arch>`` files byte for byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# app/org.gnome.Foo/x86_64/stable, runtime/org.gnome.Platform/x86_64/47, ...
_REF_RE = re.compile(r"^(app|runtime)/([^/]+)/([^/]+)/([^/]+)$")


@dataclass(frozen=True)
class SummaryRef:
    kind: str  # "app" or "runtime"
    app_id: str
    arch: str
    branch: str
    checksum: str | None  # hex sha256 of the ref's current commit, when present


def _offset_size(container_len: int) -> int:
    """Framing offsets are stored just wide enough to address every byte boundary
    in their own container (spec section 2.3.6): 0 bytes for an empty container,
    otherwise the smallest of 1/2/4 bytes that can index it."""
    if container_len == 0:
        return 0
    if container_len <= 0xFF:
        return 1
    if container_len <= 0xFFFF:
        return 2
    return 4


def _read_uint(buf: bytes, pos: int, size: int) -> int:
    return int.from_bytes(buf[pos:pos + size], "little")


def _align(pos: int, alignment: int) -> int:
    rem = pos % alignment
    return pos if rem == 0 else pos + (alignment - rem)


def parse_refs(data: bytes) -> list[SummaryRef]:
    """Every app/runtime ref an OSTree summary file lists. Returns ``[]`` for
    anything that doesn't parse as this specific format, rather than raising -
    a malformed or unexpected response from a third-party remote should be
    skipped, not crash the crawl."""
    if not data:
        return []
    try:
        return _parse(data)
    except (IndexError, UnicodeDecodeError):
        return []


def _parse(data: bytes) -> list[SummaryRef]:
    # Outer tuple (a(s(taya{sv})) a{sv}): two members. The second (top-level
    # metadata) is last, so it needs no offset of its own; the array (not last)
    # gets exactly one trailing offset, sized for the whole file.
    outer_off_size = _offset_size(len(data))
    if outer_off_size == 0 or len(data) < outer_off_size:
        return []
    array_end = _read_uint(data, len(data) - outer_off_size, outer_off_size)
    if array_end > len(data):
        return []
    return _parse_ref_array(data[:array_end])


def _parse_ref_array(buf: bytes) -> list[SummaryRef]:
    """``a(s(taya{sv}))``: GVariant arrays of non-fixed-size elements store one
    trailing offset per element, in order (not reversed - that rule is for
    structures). The last offset marks where the offset table itself begins, so
    the element count falls out of ``(len(buf) - last_offset) / offset_size``
    without needing to know it up front."""
    off_size = _offset_size(len(buf))
    if off_size == 0 or len(buf) < off_size:
        return []
    last_offset = _read_uint(buf, len(buf) - off_size, off_size)
    if last_offset > len(buf):
        return []
    table = buf[last_offset:]
    ends = [_read_uint(table, i * off_size, off_size) for i in range(len(table) // off_size)]

    refs: list[SummaryRef] = []
    start = 0
    for end in ends:
        if end < start or end > last_offset:
            break  # malformed; stop rather than guess
        ref = _parse_ref_entry(buf, start, end)
        if ref is not None:
            refs.append(ref)
        start = _align(end, 8)  # (s(taya{sv})) has alignment 8 (from the nested tuple)
    return refs


def _parse_ref_entry(buf: bytes, start: int, end: int) -> SummaryRef | None:
    """One ``(s(taya{sv}))`` element. The string is the tuple's one non-fixed,
    non-last member, so it gets exactly one trailing offset within [start, end)."""
    length = end - start
    if length <= 0:
        return None
    off_size = _offset_size(length)
    if off_size == 0 or length < off_size:
        return None
    name_end = start + _read_uint(buf, end - off_size, off_size)
    if name_end <= start or name_end > end:
        return None
    name = buf[start:name_end - 1].decode("utf-8")  # drop the string's NUL terminator
    m = _REF_RE.match(name)
    if not m:
        return None

    # (t, ay, a{sv}): an 8-byte commit size, then the commit's checksum. "ay" is
    # technically a non-fixed GVariant type, but OSTree only ever writes a 32-byte
    # sha256 into it - a format invariant of OSTree's, not a general GVariant one -
    # so this fixed offset is safe without decoding the array's own framing.
    body_start = _align(name_end, 8)
    checksum = None
    if body_start + 8 + 32 <= end:
        checksum = buf[body_start + 8:body_start + 8 + 32].hex()
    return SummaryRef(kind=m.group(1), app_id=m.group(2), arch=m.group(3), branch=m.group(4), checksum=checksum)
