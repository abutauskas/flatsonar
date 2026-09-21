"""ostree_summary's GVariant reader.

encode_summary() below is a minimal, test-only encoder for the exact same format
(the inverse of what the module decodes) so these tests are self-contained and can
exercise edge cases without a network fixture. The decoder itself was additionally
verified by hand against a real remote (GNOME Nightly's live summary file): every
checksum it extracted for the appstream2/* refs matched the value independently
fetched from that remote's refs/heads/appstream2/<arch> files, byte for byte - see
ostree_summary.py's module docstring.
"""

from flatsonar_core.ostree_summary import SummaryRef, _align, _offset_size, parse_refs

# Every fixture below stays under 256 bytes total, so 1-byte offsets throughout;
# _offset_size's boundary selection is covered separately below.
_OFF = 1


def _encode_ref_entry(name: str, checksum: bytes | None) -> bytes:
    """One ``(s(taya{sv}))`` element: a ref name, then (commit size, checksum,
    empty metadata dict). The string is the tuple's one non-fixed, non-last
    member, so it gets exactly one trailing offset."""
    name_bytes = name.encode() + b"\x00"
    name_end = len(name_bytes)
    body = b"\x00" * 8  # t: commit size - unused by the reader, value doesn't matter
    body += checksum or b"\x00" * 32  # ay: sha256 checksum
    pad = -name_end % 8  # align the (t,ay,a{sv}) tuple to 8
    entry = name_bytes + b"\x00" * pad + body  # a{sv} is empty: 0 bytes, nothing to append
    entry += name_end.to_bytes(_OFF, "little")
    return entry


def encode_summary(refs: list[tuple[str, bytes | None]]) -> bytes:
    """A full ``(a(s(taya{sv}))a{sv})`` summary listing the given (ref, checksum) pairs."""
    array_buf = b""
    ends = []
    for name, checksum in refs:
        pad = -len(array_buf) % 8  # (s(taya{sv})) tuples align to 8
        array_buf += b"\x00" * pad
        array_buf += _encode_ref_entry(name, checksum)
        ends.append(len(array_buf))
    for end in ends:
        array_buf += end.to_bytes(_OFF, "little")
    # Outer tuple: member 0 (the array) is non-fixed and not last, so it gets one
    # trailing offset; member 1 (top-level metadata, "a{sv}") is empty and last.
    return array_buf + len(array_buf).to_bytes(_OFF, "little")


CHECKSUM = bytes(range(32))


def test_parses_app_and_runtime_refs():
    data = encode_summary([
        ("app/org.gnome.Foo/x86_64/stable", CHECKSUM),
        ("runtime/org.gnome.Platform/x86_64/47", None),
    ])
    refs = parse_refs(data)
    assert refs == [
        SummaryRef("app", "org.gnome.Foo", "x86_64", "stable", CHECKSUM.hex()),
        SummaryRef("runtime", "org.gnome.Platform", "x86_64", "47", (b"\x00" * 32).hex()),
    ]


def test_non_app_non_runtime_refs_are_filtered_out():
    data = encode_summary([
        ("appstream2/x86_64", CHECKSUM),
        ("app/org.gnome.Foo/x86_64/stable", CHECKSUM),
    ])
    refs = parse_refs(data)
    assert [r.app_id for r in refs] == ["org.gnome.Foo"]


def test_empty_summary():
    assert parse_refs(encode_summary([])) == []
    assert parse_refs(b"") == []


def test_garbage_input_does_not_raise():
    assert parse_refs(b"\xff" * 10) == []
    assert parse_refs(b"not a summary file at all") == []


def test_several_refs_round_trip():
    # Stays under 256 bytes total, matching this fixture's fixed 1-byte offset
    # assumption; _offset_size's own boundary selection is tested separately.
    pairs = [(f"app/org.x.A{i}/x86_64/stable", bytes([i]) * 32) for i in range(3)]
    refs = parse_refs(encode_summary(pairs))
    assert len(refs) == 3
    assert {r.app_id for r in refs} == {f"org.x.A{i}" for i in range(3)}
    assert refs[2].checksum == (bytes([2]) * 32).hex()


def test_offset_size_boundaries():
    assert _offset_size(0) == 0
    assert _offset_size(1) == 1
    assert _offset_size(0xFF) == 1
    assert _offset_size(0x100) == 2
    assert _offset_size(0xFFFF) == 2
    assert _offset_size(0x10000) == 4


def test_align():
    assert _align(0, 8) == 0
    assert _align(1, 8) == 8
    assert _align(8, 8) == 8
    assert _align(9, 8) == 16
