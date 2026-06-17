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
storage/               transfer destination — create or symlink to your target (gitignored)
Makefile               lifecycle management (make install / run / restart / clean / status)
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
sudo apt install -y pipx ffmpeg rsync v4l-utils

# 2. Install uv — the Python package/venv manager used by this project.
#    (pipx installs it into an isolated env so it doesn't pollute the system.)
pipx install uv

# 3. Build the native Magewell library. The vendored SDK archive is committed
#    to the repo; this script unpacks it and compiles libMWCapture.so into
#    packages/magewell/src/magewell/_lib/ (gitignored — rebuild per box).
packages/magewell/build_lib.sh

# 4. Run the test suite. Layout tests always run; device tests require the
#    Magewell to be plugged in and the udev rule to be active.
uv run pytest

# Simple one-shot capture (runs until Ctrl-C; output goes to ~/Downloads)
.venv/bin/python scripts/capture.py            # until Ctrl-C
.venv/bin/python scripts/capture.py -d 60      # 60-second timed capture
.venv/bin/python scripts/capture.py -o /tmp    # custom output directory
```

## Lifecycle

`make` manages three states: **CLEAN** (bare checkout), **RUN** (hardware set up,
running interactively), and **INSTALL** (running as a systemd service).

```
CLEAN ←──────────── make clean ────────────→ CLEAN
  │                                             ↑
make install / make run                      make clean
  │                                             │
  ↓                                             │
RUN ←───── make run ───────────────────────────┤
  │                                             │
make install                                 make clean
  │                                             │
  ↓                                             │
INSTALL ←── make restart ── INSTALL ───────────┘
```

| Command | What it does |
|---|---|
| `make` | Show current state (udev rules, venv, service status) |
| `make install` | Install udev rules + venv + systemd service, then start. No-op if already running. |
| `make run` | Installs udev rules if missing, stops service if running, launches interactively. |
| `make restart` | Restart the running service (errors if not running). |
| `make clean` | Stop + remove service and udev rules; delete generated files. |

## Configuration

Two gitignored directories live at the repo root:

- `sessions/` — scratch space for raw captures and extracted recordings. Created automatically on first run.
- `storage/` — transfer destination for the Transfer button on `/view`. Create this directory or symlink it to wherever you want recordings to land:

```bash
ln -s /mnt/jarvis-incoming storage
```

If `storage/` does not exist, the Transfer button returns an error until it is created or symlinked. Use `--storage-dir DIR` to override the path at the command line.

## How it works

Both scripts share the same signal probe and encoding logic via `capture_shared.py`.

### Simple capture (`capture.py`)

1. Probes the live HDMI input via the `magewell` binding — the SDK reads the
   true input resolution, frame rate, and interlace status.
2. Falls back to 1920×1080@60 if no locked signal is detected.
3. Launches ffmpeg: V4L2 YUYV 4:2:2 + direct ALSA audio → HEVC NVENC (Main10,
   VBR CQ21, preset p6, 80M ceiling) + AAC 192k → timestamped MP4 with
   `faststart` (e.g. `capture_20260521_143022.mp4`).
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
5. On shutdown (Ctrl-C / SIGTERM), any open recording is closed, then ffmpeg
   extracts each marked recording from the session file using stream-copy
   (`-c copy`) — no re-encode. Output files are named
   `session_YYYYMMDD_HHMMSS_N_starting_<offset>.mp4` where `<offset>` is the
   start time within the session (e.g. `2m30s`, `1h15m4s`) with leading zero
   components omitted. The session file is retained alongside the recordings.
