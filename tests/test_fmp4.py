"""Tier 1 tests for fMP4 init segment validity.

Generates a short fMP4 using ffmpeg + libx265 (software encoder) and validates
the moov structure that MSE requires. Catches issues like empty hvcC boxes
without needing a real browser.
"""
from __future__ import annotations

import struct
import subprocess
import shutil

import pytest

# Skip entire module if ffmpeg or libx265 not available
pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg not found",
)


def _parse_boxes(data: bytes, offset: int = 0, end: int | None = None) -> list[tuple[str, int, int]]:
    """Parse MP4 boxes. Returns [(type, offset, size), ...]."""
    if end is None:
        end = len(data)
    boxes = []
    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        box_type = data[offset + 4:offset + 8].decode("ascii", errors="replace")
        if size < 8:
            break
        boxes.append((box_type, offset, size))
        offset += size
    return boxes


def _find_box(data: bytes, path: list[str], offset: int = 0, end: int | None = None) -> tuple[int, int] | None:
    """Find a nested box by path (e.g. ['moov', 'trak', 'mdia']).
    Returns (offset, size) of the innermost box, or None."""
    if end is None:
        end = len(data)
    target = path[0]
    remaining = path[1:]

    boxes = _parse_boxes(data, offset, end)
    for box_type, box_offset, box_size in boxes:
        if box_type == target:
            if not remaining:
                return box_offset, box_size
            # container box header is 8 bytes; stsd has 8 extra bytes
            header = 16 if target == "stsd" else 8
            return _find_box(data, remaining, box_offset + header, box_offset + box_size)
    return None


@pytest.fixture(scope="module")
def fmp4_data() -> bytes:
    """Generate a short fMP4 using ffmpeg + libx265."""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=3",
            "-f", "lavfi", "-i", "sine=f=1000:r=48000:d=3",
            "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "log-level=error:keyint=30",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "mp4",
            "-movflags", "+frag_keyframe+default_base_moof",
            "pipe:1",
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr.decode()}"
    assert len(result.stdout) > 100, "fMP4 output too small"
    return result.stdout


def test_starts_with_ftyp(fmp4_data):
    boxes = _parse_boxes(fmp4_data, 0, min(len(fmp4_data), 100))
    assert boxes[0][0] == "ftyp"


def test_has_moov(fmp4_data):
    result = _find_box(fmp4_data, ["moov"])
    assert result is not None, "no moov box found"


def test_has_moof(fmp4_data):
    """fMP4 must have at least one moof (fragment)."""
    boxes = _parse_boxes(fmp4_data)
    moof_boxes = [b for b in boxes if b[0] == "moof"]
    assert len(moof_boxes) >= 1, "no moof fragment found"


def test_moov_has_mvex(fmp4_data):
    """fMP4 moov must contain mvex (movie extends) for fragmented playback."""
    result = _find_box(fmp4_data, ["moov", "mvex"])
    assert result is not None, "moov missing mvex box"


def test_moov_has_video_track(fmp4_data):
    result = _find_box(fmp4_data, ["moov", "trak", "mdia", "minf", "stbl", "stsd"])
    assert result is not None, "no video stsd found"


def test_hevc_sample_entry_present(fmp4_data):
    """The stsd must contain an hev1 or hvc1 sample entry."""
    stsd = _find_box(fmp4_data, ["moov", "trak", "mdia", "minf", "stbl", "stsd"])
    assert stsd is not None
    stsd_offset, stsd_size = stsd
    # stsd: 8 byte header + 4 version + 4 entry_count + entries
    entries = _parse_boxes(fmp4_data, stsd_offset + 16, stsd_offset + stsd_size)
    entry_types = [e[0] for e in entries]
    assert "hev1" in entry_types or "hvc1" in entry_types, (
        f"no HEVC sample entry found, got: {entry_types}"
    )


def test_hvcc_is_not_empty(fmp4_data):
    """The hvcC (HEVC decoder config) must contain actual codec parameters.

    An empty hvcC (size=8, header only) means ffmpeg wrote the moov before
    processing any frames (empty_moov flag). MSE requires VPS/SPS/PPS in
    hvcC to initialize the decoder.
    """
    # Find hev1 or hvc1 entry
    stsd = _find_box(fmp4_data, ["moov", "trak", "mdia", "minf", "stbl", "stsd"])
    assert stsd is not None
    stsd_offset, stsd_size = stsd
    entries = _parse_boxes(fmp4_data, stsd_offset + 16, stsd_offset + stsd_size)

    hevc_entry = None
    for etype, eoff, esize in entries:
        if etype in ("hev1", "hvc1"):
            hevc_entry = (eoff, esize)
            break
    assert hevc_entry is not None

    # hev1/hvc1 sample entry: 8 header + 78 fixed fields = sub-boxes at +86
    eoff, esize = hevc_entry
    sub_boxes = _parse_boxes(fmp4_data, eoff + 86, eoff + esize)
    hvcc = None
    for btype, boff, bsize in sub_boxes:
        if btype == "hvcC":
            hvcc = (boff, bsize)
            break

    assert hvcc is not None, "no hvcC box in HEVC sample entry"
    _, hvcc_size = hvcc
    assert hvcc_size > 8, (
        f"hvcC is empty (size={hvcc_size}). This means the moov was written "
        f"before ffmpeg processed any frames (empty_moov). MSE needs "
        f"VPS/SPS/PPS in hvcC. Remove empty_moov from movflags."
    )
