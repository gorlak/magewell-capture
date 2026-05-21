#!/home/geoff/Projects/magewell-capture/.venv/bin/python
"""capture.py — loose, editable capture orchestrator (Phase 1 stub).

Planned behavior (see DECISIONS.md):
  1. probe the live signal via `magewell` (res / fps / interlaced / lock state)
  2. build the ffmpeg command — detected params, with a 1920x1080@60 fallback
  3. run ffmpeg: V4L2 yuyv422 -> nv12 -> h264_nvenc, + ALSA hw:CARD=HDMI,DEV=0
     -> timestamped file in ~/captures
  4. graceful SIGINT shutdown

Run via the workspace venv (this file's shebang points at it), e.g.:
    .venv/bin/python scripts/capture.py
"""
from __future__ import annotations

import magewell


def main() -> int:
    print(f"magewell {magewell.__version__} — capture.py stub; pipeline TBD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
