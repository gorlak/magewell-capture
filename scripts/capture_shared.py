"""Shared logic for capture scripts (capture.py, monitor.py).

Contains signal probing, ffmpeg command building, output path generation,
and device/encoding constants. See DECISIONS.md § "Capture pipeline".
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import magewell

# ---------------------------------------------------------------------------
# Defaults (from DECISIONS.md)
# ---------------------------------------------------------------------------

FALLBACK_WIDTH = 1920
FALLBACK_HEIGHT = 1080
FALLBACK_FPS = 60

VIDEO_DEVICE = "/dev/video0"
AUDIO_DEVICE = "hw:CARD=HDMI,DEV=0"

DEFAULT_OUTPUT_DIR = Path.home() / "Downloads"


# ---------------------------------------------------------------------------
# Signal probe
# ---------------------------------------------------------------------------

def probe_signal() -> tuple[int, int, float, bool]:
    """Return (width, height, fps, interlaced) from the live input, or fallback.

    Prints what it detected so the operator can verify before a long capture.
    """
    try:
        sig = magewell.read_signal()
    except magewell.MWError as exc:
        print(f"[probe] SDK read failed ({exc}); using fallback", file=sys.stderr)
        return FALLBACK_WIDTH, FALLBACK_HEIGHT, FALLBACK_FPS, False

    if not sig.locked:
        print(f"[probe] no locked signal (state={sig.state.name}); using fallback",
              file=sys.stderr)
        return FALLBACK_WIDTH, FALLBACK_HEIGHT, FALLBACK_FPS, False

    print(f"[probe] detected: {sig}")
    print(f"  color={sig.color_format.name}  quant={sig.quant_range.name}  "
          f"sat={sig.sat_range.name}  frame={sig.frame_type.name}")

    try:
        audio = magewell.read_audio_signal()
        print(f"  audio: {audio}")
    except magewell.MWError:
        print("  audio: (could not read)", file=sys.stderr)

    return sig.width, sig.height, sig.fps, sig.interlaced


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def fps_to_ffmpeg(fps: float) -> str:
    """Convert a float fps to the best ffmpeg -framerate string.

    Common NTSC rates map to exact ratios; others pass through as-is.
    """
    _RATIOS = {
        23.976: "24000/1001",
        29.97:  "30000/1001",
        59.94:  "60000/1001",
    }
    for ref, ratio in _RATIOS.items():
        if abs(fps - ref) < 0.01:
            return ratio
    if fps == int(fps):
        return str(int(fps))
    return f"{fps:.3f}"


def build_input_args(
    width: int, height: int, fps: float,
    video_device: str = VIDEO_DEVICE,
    audio_device: str = AUDIO_DEVICE,
) -> list[str]:
    """Build ffmpeg input arguments for V4L2 video + ALSA audio."""
    fps_str = fps_to_ffmpeg(fps)
    cmd: list[str] = []

    # ---- video input ----
    cmd += [
        "-thread_queue_size", "1024",
        "-f", "v4l2",
        "-input_format", "yuyv422",
        "-video_size", f"{width}x{height}",
        "-framerate", fps_str,
        "-use_wallclock_as_timestamps", "1",
        "-i", video_device,
    ]

    # ---- audio input (direct ALSA — not PipeWire) ----
    # NOTE: do NOT use -use_wallclock_as_timestamps here. ALSA provides
    # accurate timestamps from the hardware sample counter; overriding with
    # wall clock causes mis-timestamped bursts that aresample=async turns
    # into silence gaps (audio plays a few samples then drops out).
    cmd += [
        "-thread_queue_size", "1024",
        "-f", "alsa",
        "-ac", "2",
        "-ar", "48000",
        "-i", audio_device,
    ]

    return cmd


def build_encode_args(fps: float) -> list[str]:
    """Build ffmpeg encode arguments (HEVC NVENC + AAC)."""
    gop = round(fps * 2)  # 2-second GOP

    cmd: list[str] = []

    # ---- video encode (single NVENC) ----
    cmd += [
        "-c:v", "hevc_nvenc",
        "-preset", "p6",
        "-rc", "vbr",
        "-cq", "21",
        "-b:v", "0",
        "-maxrate", "80M",
        "-bufsize", "160M",
        "-profile:v", "main10",
        "-pix_fmt", "nv12",
        "-spatial-aq", "1",
        "-g", str(gop),
    ]

    # ---- audio encode ----
    cmd += [
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", "aresample=async=1000",
    ]

    # ---- mapping ----
    cmd += ["-map", "0:v", "-map", "1:a"]

    return cmd


# ---------------------------------------------------------------------------
# ffmpeg command builders
# ---------------------------------------------------------------------------

def build_capture_cmd(
    width: int,
    height: int,
    fps: float,
    interlaced: bool,
    output: Path,
    duration: float | None = None,
    video_device: str = VIDEO_DEVICE,
    audio_device: str = AUDIO_DEVICE,
) -> list[str]:
    """Build ffmpeg argv for a simple one-shot capture to file."""
    cmd: list[str] = ["ffmpeg", "-hide_banner"]
    cmd += build_input_args(width, height, fps, video_device, audio_device)

    if duration is not None:
        cmd += ["-t", str(duration)]

    cmd += build_encode_args(fps)
    cmd += ["-movflags", "+faststart", str(output)]

    return cmd


def build_monitor_cmd(
    width: int,
    height: int,
    fps: float,
    interlaced: bool,
    output: Path,
    pipe_fd: int | str = "pipe:1",
    video_device: str = VIDEO_DEVICE,
    audio_device: str = AUDIO_DEVICE,
) -> list[str]:
    """Build ffmpeg argv for continuous capture with dual output.

    Two separate encode+mux paths (the tee muxer doesn't forward codec
    extradata, producing an empty hvcC that MSE rejects):
      1. MP4 file with faststart (recording)
      2. Fragmented MP4 to pipe for WebSocket+MSE preview

    Uses two NVENC sessions (T400 supports 3 concurrent).
    """
    cmd: list[str] = ["ffmpeg", "-hide_banner"]
    cmd += build_input_args(width, height, fps, video_device, audio_device)

    encode = build_encode_args(fps)

    # ---- output 1: recording file ----
    cmd += encode
    cmd += ["-movflags", "+faststart", str(output)]

    # ---- output 2: fMP4 pipe for WebSocket preview ----
    cmd += encode
    cmd += [
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", pipe_fd,
    ]

    return cmd


def build_extract_cmd(
    source: Path,
    start: float,
    end: float,
    output: Path,
) -> list[str]:
    """Build ffmpeg argv for stream-copy extraction of a segment.

    A small pad (0.15s) is added to the end to avoid truncating the last
    audio packet — with -c copy, cuts happen at packet boundaries and the
    final audio frame can be dropped if it straddles the cut point.
    """
    return [
        "ffmpeg", "-hide_banner",
        "-ss", f"{start:.3f}",
        "-to", f"{end + 0.15:.3f}",
        "-i", str(source),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ]


# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------

def make_output_path(output_dir: Path, width: int, height: int,
                     fps: float, interlaced: bool,
                     prefix: str = "capture") -> Path:
    """Generate a timestamped output filename like:
    capture_20260521_143022_1920x1080p60.mp4
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan = "i" if interlaced else "p"
    fps_tag = f"{fps:g}"
    name = f"{prefix}_{ts}_{width}x{height}{scan}{fps_tag}.mp4"
    return output_dir / name
