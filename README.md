# magewell-capture

Headless HDMI capture + archival appliance built around a Magewell USB Capture
HDMI Gen2. A `magewell` Python package wraps the Magewell MWCapture SDK (via
ctypes) to read the live input signal; capture/encode is done by ffmpeg (NVENC).

See **[DECISIONS.md](DECISIONS.md)** for the full design record, findings, and
rationale, and **[ATTRIBUTIONS.md](ATTRIBUTIONS.md)** for third-party credits.

## Layout

```
packages/magewell/   ctypes wrapper + vendored SDK + native-lib build script
scripts/             loose, editable capture scripts (run via the venv)
setup.sh             one-time privileged setup (udev rule) — undo: teardown.sh
DECISIONS.md         design record
ATTRIBUTIONS.md      third-party / consulted-project credits
```

The Magewell SDK license notice lives with the vendored SDK it governs
(`packages/magewell/vendor/mwcapture-sdk-<ver>/LICENSE.txt`).

## Bootstrap (Debian 13 / this box)

```bash
# tooling (one-time)
sudo apt install -y pipx ffmpeg v4l-utils
pipx install uv

# one-time privileged setup — installs a udev rule so the capture user (group
# plugdev) can reach the Magewell's device nodes (raw USB + HID), which the SDK
# opens read-write to read signal status (otherwise: MWAccessError / EACCES).
# The script prints the exact rule before installing it; read it first if you
# like. Root is needed only to write under /etc/udev and reload udev — no
# downloads, no network.  (Undo any time with:  sudo ./teardown.sh)
sudo ./setup.sh

# build the native Magewell lib, then create the venv with all packages
packages/magewell/build_lib.sh
uv sync

# run the tests (layout tests always run; device tests need the Magewell attached)
uv run pytest

# capture (runs until Ctrl-C; use -d for a timed capture)
.venv/bin/python scripts/capture.py            # until Ctrl-C
.venv/bin/python scripts/capture.py -d 60      # 60-second capture
.venv/bin/python scripts/capture.py -o /tmp    # custom output dir (default: ~/Downloads)
# (optional) expose on PATH:  ln -s "$PWD/scripts/capture.py" ~/.local/bin/capture
```

The native `libMWCapture.so` is built per-box from the vendored SDK archive and
is gitignored; everything needed to rebuild it is committed.

## How it works

1. `capture.py` probes the live HDMI input via the `magewell` binding (SDK
   reads the true input resolution, frame rate, and interlace status).
2. If a locked signal is detected, ffmpeg is launched with matching parameters;
   otherwise it falls back to 1920×1080@60.
3. Video: V4L2 YUYV 4:2:2 → nv12 → h264_nvenc (VBR CQ21, preset p6,
   spatial + temporal AQ, 80M ceiling). Audio: direct ALSA → AAC 192k.
4. Output: timestamped MP4 with `faststart` (e.g.
   `capture_20260521_143022_1920x1080p59.946.mp4`).
5. Ctrl-C (SIGINT) triggers a clean ffmpeg shutdown — the file is finalized and
   playable.
