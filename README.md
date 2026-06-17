# magewell-capture

Headless HDMI capture + archival appliance built around a Magewell USB Capture
HDMI Gen2. A `magewell` Python package wraps the Magewell MWCapture SDK (via
ctypes) to read the live input signal; capture/encode is done by ffmpeg (NVENC).

See **[DECISIONS.md](DECISIONS.md)** for the full design record, findings, and
rationale, and **[ATTRIBUTIONS.md](ATTRIBUTIONS.md)** for third-party credits.

## Layout

```
packages/magewell/     ctypes wrapper + vendored SDK + native-lib build script
scripts/               capture scripts (run via the venv)
  capture.py           simple one-shot capture to file
  monitor.py           monitored capture with browser preview and record control
  capture_shared.py    shared signal probe + ffmpeg command builders
  web/index.html       browser UI served by monitor.py
sessions/              runtime scratch space — session and recording files (gitignored)
config.toml            local config (gitignored; see config.toml.TEMPLATE to get started)
config.toml.TEMPLATE   self-documented config template — copy to config.toml and edit
setup.sh               one-time privileged setup (udev rule) — undo: teardown.sh
DECISIONS.md           design record
ATTRIBUTIONS.md        third-party / consulted-project credits
```

The Magewell SDK license notice lives with the vendored SDK it governs
(`packages/magewell/vendor/mwcapture-sdk-<ver>/LICENSE.txt`).

## Bootstrap (developed in Debian 13)

```bash
# 1. Install system tools (one-time):
#    - pipx: isolated Python app installer, used here only to install uv
#    - ffmpeg: encoding pipeline (NVENC, fMP4 output, segment extraction)
#    - v4l-utils: optional but useful for inspecting V4L2 device capabilities
sudo apt install -y pipx ffmpeg v4l-utils

# 2. Install uv — the Python package/venv manager used by this project.
#    (pipx installs it into an isolated env so it doesn't pollute the system.)
pipx install uv

# 3. One-time privileged setup: installs a udev rule so the capture user
#    (group plugdev) can access the Magewell's device nodes (raw USB + HID).
#    The SDK opens these read-write to read signal status; without the rule
#    you get MWAccessError / EACCES. The script prints the exact rule before
#    writing it. Root is needed only to write under /etc/udev and reload udev
#    — no downloads, no network. (Undo any time with: sudo ./teardown.sh)
sudo ./setup.sh

# 4. Build the native Magewell library. The vendored SDK archive is committed
#    to the repo; this script unpacks it and compiles libMWCapture.so into
#    packages/magewell/src/magewell/_lib/ (gitignored — rebuild per box).
packages/magewell/build_lib.sh

# 5. Create the venv and install all Python dependencies (magewell package,
#    websockets, pytest, etc.) as declared in pyproject.toml.
uv sync

# 6. Run the test suite. Layout tests always run; device tests require the
#    Magewell to be plugged in and the udev rule to be active.
uv run pytest

# Simple one-shot capture (runs until Ctrl-C; output goes to ~/Downloads)
.venv/bin/python scripts/capture.py            # until Ctrl-C
.venv/bin/python scripts/capture.py -d 60      # 60-second timed capture
.venv/bin/python scripts/capture.py -o /tmp    # custom output directory

# Monitored capture with browser preview and record control
.venv/bin/python scripts/monitor.py            # serves on http://localhost:8090
.venv/bin/python scripts/monitor.py -p 9090    # custom port

# (optional) expose on PATH:  ln -s "$PWD/scripts/capture.py" ~/.local/bin/capture
```

## Configuration

`monitor.py` reads an optional `config.toml` in the repo root (gitignored).
Copy the template to get started:

```bash
cp config.toml.TEMPLATE config.toml
# edit config.toml — set transfer_dest to your desired destination
```

Currently the only supported key is:

| Key | Description |
|-----|-------------|
| `transfer_dest` | Absolute path to copy each finished recording to after extraction. The local copy in `sessions/` is removed on success. Defaults to `~/Downloads` if absent. |

Example:

```toml
transfer_dest = "/mnt/jarvis-incoming"
```

`sessions/` (the scratch directory for raw session captures and pre-transfer
recordings) is also gitignored. It is created automatically on first run.

## How it works

Both scripts share the same signal probe and encoding logic via `capture_shared.py`.

### Simple capture (`capture.py`)

1. Probes the live HDMI input via the `magewell` binding — the SDK reads the
   true input resolution, frame rate, and interlace status.
2. Falls back to 1920×1080@60 if no locked signal is detected.
3. Launches ffmpeg: V4L2 YUYV 4:2:2 + direct ALSA audio → HEVC NVENC (Main10,
   VBR CQ21, preset p6, 80M ceiling) + AAC 192k → timestamped MP4 with
   `faststart` (e.g. `capture_20260521_143022_1920x1080p60.mp4`).
4. Ctrl-C is handled by letting ffmpeg receive SIGINT directly (same process
   group); Python ignores the signal and waits. This ensures ffmpeg completes
   its `faststart` second pass and the file is properly finalized.

### Monitored capture (`monitor.py`)

1. Same signal probe and HEVC NVENC encoding as above, but ffmpeg runs with
   **dual output**: a session MP4 file written to disk simultaneously with a
   fragmented MP4 stream piped to stdout.
2. An asyncio HTTP server (`:8090`) serves the browser UI (`web/index.html`)
   and a small JSON API: `GET /api/status`, `GET /api/mark-in?t=`, `GET
   /api/mark-out?t=`.
3. An asyncio WebSocket server (`:8091`) reads the fMP4 pipe from ffmpeg and
   fans each fragment out to all connected browser clients. Late-joining clients
   receive the cached init segment (moov box) so playback starts immediately.
4. The browser UI plays the live stream via MediaSource Extensions (MSE), shows
   a timecode overlay driven by `video.currentTime`, and displays a
   STANDBY/RECORDING badge. The Record button (or Space bar) sends mark-in and
   mark-out requests with the browser's current stream timestamp.
5. On shutdown (Ctrl-C / SIGTERM), any open recording segment is closed, then
   ffmpeg extracts each marked segment from the session file using stream-copy
   (`-c copy`) — no re-encode. The session file is retained alongside the
   extracted recordings.
