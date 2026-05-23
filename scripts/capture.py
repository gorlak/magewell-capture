#!/home/geoff/Projects/magewell-capture/.venv/bin/python
"""capture.py — simple one-shot HDMI capture.

Probes the live input signal via the magewell binding, builds an ffmpeg command
matching the detected format (with a 1920x1080@60 fallback), and runs the
capture to a timestamped file in ~/Downloads.

For monitored capture with browser preview and record control, see monitor.py.
See DECISIONS.md § "Capture pipeline" for the design rationale.

Usage:
    .venv/bin/python scripts/capture.py [--duration SECONDS] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from pathlib import Path

from capture_shared import (
    DEFAULT_OUTPUT_DIR,
    build_capture_cmd,
    make_output_path,
    probe_signal,
)


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
    cmd = build_capture_cmd(width, height, fps, interlaced, output, args.duration)
    print(f"\n[capture] output: {output}")
    print(f"[capture] cmd: {' '.join(cmd)}\n")

    # ---- run ffmpeg ----
    # ffmpeg handles SIGINT (sends 'q') gracefully and finalizes the file.
    # Since ffmpeg is in the same process group it receives SIGINT directly
    # from the terminal — we must NOT forward it again or ffmpeg gets a
    # double signal that interrupts the faststart second pass and corrupts
    # the file. Python ignores SIGINT and just waits for ffmpeg to exit.
    proc = subprocess.Popen(cmd)

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

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
