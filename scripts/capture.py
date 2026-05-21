#!/home/geoff/Projects/magewell-capture/.venv/bin/python
"""capture.py — HDMI capture orchestrator.

Probes the live input signal via the magewell binding, builds an ffmpeg command
matching the detected format (with a 1920x1080@60 fallback), and runs the
capture to a timestamped file in ~/captures.

See DECISIONS.md § "Capture pipeline" for the design rationale.

Usage:
    .venv/bin/python scripts/capture.py [--duration SECONDS] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
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
# ffmpeg command builder
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(
    width: int,
    height: int,
    fps: float,
    interlaced: bool,
    output: Path,
    duration: float | None = None,
) -> list[str]:
    """Build the ffmpeg argv for a single-NVENC capture to file.

    Video: V4L2 yuyv422 → nv12 → hevc_nvenc (VBR CQ21, p6, spatial+temporal AQ).
    Audio: direct ALSA → AAC 192k, aresample async safety net.
    """
    # choose a framerate string ffmpeg accepts (integer or ratio)
    # 59.94 → 60000/1001; 29.97 → 30000/1001; otherwise use the float
    fps_str = _fps_to_ffmpeg(fps)
    gop = round(fps * 2)  # 2-second GOP for later HLS segmentation

    cmd: list[str] = ["ffmpeg", "-hide_banner"]

    # ---- video input ----
    cmd += [
        "-thread_queue_size", "1024",
        "-f", "v4l2",
        "-input_format", "yuyv422",
        "-video_size", f"{width}x{height}",
        "-framerate", fps_str,
        "-use_wallclock_as_timestamps", "1",
        "-i", VIDEO_DEVICE,
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
        "-i", AUDIO_DEVICE,
    ]

    # ---- duration limit (optional) ----
    if duration is not None:
        cmd += ["-t", str(duration)]

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

    # ---- output ----
    cmd += [
        "-movflags", "+faststart",
        str(output),
    ]

    return cmd


def _fps_to_ffmpeg(fps: float) -> str:
    """Convert a float fps to the best ffmpeg -framerate string.

    Common NTSC rates map to exact ratios; others pass through as-is.
    """
    _RATIOS = {
        23.976: "24000/1001",
        29.97:  "30000/1001",
        59.94:  "60000/1001",
    }
    # match within 0.01 tolerance
    for ref, ratio in _RATIOS.items():
        if abs(fps - ref) < 0.01:
            return ratio
    # exact integer
    if fps == int(fps):
        return str(int(fps))
    return f"{fps:.3f}"


# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------

def make_output_path(output_dir: Path, width: int, height: int,
                     fps: float, interlaced: bool) -> Path:
    """Generate a timestamped output filename like:
    capture_20260521_143022_1920x1080p60.mp4
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan = "i" if interlaced else "p"
    fps_tag = f"{fps:g}"
    name = f"capture_{ts}_{width}x{height}{scan}{fps_tag}.mp4"
    return output_dir / name


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture HDMI input to an NVENC-encoded file."
    )
    parser.add_argument(
        "-d", "--duration", type=float, default=None,
        help="Capture duration in seconds (default: until Ctrl-C)."
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR})."
    )
    args = parser.parse_args()

    # ---- probe ----
    width, height, fps, interlaced = probe_signal()

    # ---- output path ----
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = make_output_path(args.output_dir, width, height, fps, interlaced)

    # ---- build command ----
    cmd = build_ffmpeg_cmd(width, height, fps, interlaced, output, args.duration)
    print(f"\n[capture] output: {output}")
    print(f"[capture] cmd: {' '.join(cmd)}\n")

    # ---- run ffmpeg ----
    # ffmpeg handles SIGINT (sends 'q') gracefully and finalizes the file.
    # We forward SIGINT to the child, which is the default behavior when ffmpeg
    # is in the same process group. If we're in a pipeline or service, we
    # explicitly forward it.
    proc = subprocess.Popen(cmd)

    def _forward_signal(signum: int, _frame) -> None:
        proc.send_signal(signum)

    signal.signal(signal.SIGINT, _forward_signal)
    signal.signal(signal.SIGTERM, _forward_signal)

    rc = proc.wait()

    if rc == 0:
        size_mb = output.stat().st_size / (1024 * 1024)
        print(f"\n[capture] done: {output} ({size_mb:.1f} MB)")
    elif rc == 255:
        # ffmpeg returns 255 on SIGINT (normal Ctrl-C shutdown)
        size_mb = output.stat().st_size / (1024 * 1024) if output.exists() else 0
        print(f"\n[capture] stopped (Ctrl-C): {output} ({size_mb:.1f} MB)")
        rc = 0  # not an error
    else:
        print(f"\n[capture] ffmpeg exited with code {rc}", file=sys.stderr)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
