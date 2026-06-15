# TODO

## P0 — Data integrity (recording can be silently lost without these)

### Session file video write silently stops mid-recording
**Observed:** On a 3-hour live TV capture, the session file (.mp4) stopped being
written after ~3 minutes (243 MB at ~11 Mbps = ~3 min of full-motion video).
The fMP4 preview pipe continued streaming to the browser for the full 2.86 hours
with no visible problem.

**Why this is alarming:** The preview and the session file are the same ffmpeg
process with two outputs from the same NVENC encoder pair. They should contain
identical content. The user was watching the preview and had no idea the session
file had died. The recording is lost.

**Suspected cause:** One of the two NVENC sessions encountered a hardware/driver
error, or the pipe-deadlock stalled the muxer long enough to put the session file
into an error state. ffmpeg may have silently abandoned the session file output
while keeping the pipe output alive.

**Investigation needed:**
- Reproduce with a long recording and watch ffmpeg stderr closely for NVENC errors
- Check whether ffmpeg continues when one output fails (`-xerror` flag behaviour)
- Check whether the pipe and session file outputs share an encoder or use separate
  NVENC sessions, and what happens if one session fails

**Fixes needed:**
- Parse ffmpeg stderr during CAPTURING; match on NVENC errors and write/mux errors;
  add matched lines to the sidecar `warnings` list and surface as a banner in the UI
- Stat the session file every 10 s in a background asyncio task (single `os.stat()`
  call); alert if size has not grown while CAPTURING. This is the stall detector.
- After a 5-second settling period at CAPTURING start, verify both outputs are
  producing data before reporting "capture running" in `/api/status`


## P1 — Shutdown reliability ✅ FIXED + TESTED

### Ctrl-C hang: ws_server.wait_closed() blocks indefinitely ✅
**Fix:** `ws_server.wait_closed()` and `http_srv.wait_closed()` wrapped in a
3-second `asyncio.timeout`. Verified by
`tests/test_shutdown.py::test_shutdown_exits_within_7s_with_ws_client_connected`
— a raw socket that never sends a WebSocket Close frame (simulating a browser
tab) no longer blocks exit.

### Ctrl-C hang: ffmpeg pipe deadlock blocks moov finalisation ✅
**Fix:** On shutdown, `pipe_task` (broadcaster) is immediately cancelled and
replaced with `_drain_stdout()` (fast discard) so the pipe never blocks. ffmpeg
graceful-exit timeout extended from 15 s to 120 s. Verified by
`tests/test_shutdown.py::test_session_file_has_valid_moov_after_shutdown` —
session file has valid moov, video + audio streams, and duration ≥ 8 s after
clean shutdown.


## P2 — Full session lifecycle

The service is a long-running system process. The browser drives a four-state
session lifecycle. Each state corresponds to a distinct page.

### State flow

```
INDEX → CAPTURING → FINALIZING → REPORT → INDEX
  ↑                                         |
  └─────────────────────────────────────────┘
  ↑
  └── (abort from CAPTURING — session files kept on disk)
```

**INDEX** (`/`)
- No ffmpeg running. No WebSocket. No video tag. Screen saver can run.
- Shows all session and clip files in the output directory (see P3).
- **Start Capture** button → CAPTURING.
- Entered on: service start, REPORT → new capture, abort, unexpected ffmpeg exit.

**CAPTURING**
- ffmpeg running, WebSocket open, MSE player active, timecodes rolling.
- Record In / Record Out mark segments. Multiple segments per capture supported;
  all extracted in FINALIZING.
- Sidecar JSON written and updated atomically on every mark (see "Sidecar" below).
- `/api/status` response includes CAPTURING state, segment count, any stall/error
  warnings, and session file growth rate. Browser already polls this at 500 ms for
  the timecode display — no extra channel needed for P0 warnings.
- If ffmpeg exits unexpectedly (crash, device disconnect), server transitions
  automatically to INDEX and records the cause in the sidecar warnings.
- Closing and reopening the browser reconnects seamlessly: the fMP4 stream
  timestamps are continuous so `video.currentTime` immediately shows the real
  accumulated stream position. **Resume = reconnect the WebSocket. Nothing more.**
- Refreshing while in CAPTURING shows Resume / Abort options. Auto-abort on WS
  disconnect is intentionally NOT done (network hiccups must not destroy recordings).
- **Complete Capture** disabled (greyed out) if no segments are marked.
- **Complete Capture** → FINALIZING.

**FINALIZING**
- ffmpeg receives SIGINT; pipe drain unblocks it; moov is written.
- All child processes reaped with confirmed graceful shutdown.
- FINALIZING is driven entirely from the sidecar — no in-memory state required.
  This makes the normal path and the crash-recovery Resume path identical code.
- Each pending extraction runs in sequence; sidecar updated to `"done"` per clip.
- Optional network transfer after each clip (see P3).
- Browser polls `/api/status` at 1 s intervals. Response shape during FINALIZING:
  `{"state": "FINALIZING", "step": "extracting", "clip": 1, "total": 2}`
- No WebSocket, no video tag during FINALIZING. Screen saver can run.
- On completion → REPORT; server redirects browser to `/report`.

**REPORT** (`/report`)
- No WebSocket, no video tag. Screen saver can run.
- Shows session metadata, each clip (filename, duration, size, transfer status),
  and warnings from the sidecar.
- Clips and session file playable via `<video controls src="/files/name.mp4">` —
  HTTP range requests, no MSE. Works on iOS Safari and Android Firefox.
- REPORT is ephemeral (lost on service restart). The sidecar on INDEX is the
  durable record — it contains the same information.
- **New Capture** → INDEX.

### Abort behaviour
`/api/abort` stops ffmpeg and returns to INDEX. Session files (`.mp4` + `.json`)
are **kept on disk** — not auto-deleted. The user decides what to do with them
from the INDEX page. This preserves the option of manual recovery (e.g. `untrunc`)
after an accidental abort.

### Sidecar file

Every session gets a JSON sidecar alongside its MP4:

```
session_20260614_234256_1920x1080p60.mp4
session_20260614_234256_1920x1080p60.json
```

Written at CAPTURING start. Updated via write-to-temp + `os.replace()` (atomic)
on every mark and after each extraction step. Never auto-deleted.

```json
{
  "session_file": "session_20260614_234256_1920x1080p60.mp4",
  "started_at": "2026-06-14T23:42:56",
  "width": 1920, "height": 1080, "fps": 60, "interlaced": false,
  "segments": [
    {"in": 15.556, "out": 10304.894},
    {"in": 200.123, "out": 450.678}
  ],
  "extractions": [
    {
      "in": 15.556, "out": 10304.894,
      "output": "recording_20260614_234312_1920x1080p60.mp4",
      "transferred": "/mnt/archive/recording_20260614_234312_1920x1080p60.mp4",
      "status": "done"
    },
    {
      "in": 200.123, "out": 450.678,
      "output": null, "transferred": null,
      "status": "pending"
    }
  ],
  "warnings": []
}
```

INDEX shows a **Resume extraction** action for any sidecar with `"pending"`
extractions whose session MP4 still exists — covers FINALIZING crashes without
re-capture.

### API surface

| Endpoint | Transition | Notes |
|---|---|---|
| `GET /api/status` | — | State + progress; shape varies by state (see above) |
| `POST /api/start` | INDEX → CAPTURING | Starts ffmpeg, creates sidecar |
| `POST /api/complete` | CAPTURING → FINALIZING | 409 if no segments marked |
| `POST /api/abort` | CAPTURING → INDEX | Stops ffmpeg; files kept |
| `GET /report` | — | Only served when state is REPORT |
| `GET /files/<name>` | — | Range-request file serving; output dir only, no traversal |
| `DELETE /api/session/<name>` | — | Deletes `.mp4` + `.json` pair; validates name pattern |

`/api/mark-in` and `/api/mark-out` unchanged (CAPTURING only).

### Mobile
Portrait layout is fine — small video at top, status and controls below.
iOS Safari 17+ plays the live HEVC preview. Android Firefox cannot (no MSE HEVC)
but can use INDEX, REPORT, and clip playback via `<video src>` with hardware decode.


## P3 — Disk space, file management, and output configuration

### Configuration
Output directory and network share destination go in a config file (format and
location TBD — likely `~/.config/magewell-capture/config.toml` for user installs,
`/etc/magewell-capture/config.toml` for system service). CLI `--output-dir` flag
already exists and takes precedence.

### INDEX page: directory listing and disk status

**Disk usage bar** — free / total for the output filesystem. Refreshed on load.

**Session files** (`session_*.mp4` + `.json` pairs) — listed with size, date,
segment count, and extraction status from the sidecar. Active session (if
CAPTURING) shown as "in progress" with no Delete button. Others get a per-file
**Delete** button (removes both `.mp4` and `.json`) and a **Clear all** bulk
button. Sessions with pending extractions show **Resume extraction** instead.

**Clips** (`recording_*.mp4`) — listed read-only with size, date, transfer status.
Playable inline. No delete offered here.

No automatic eviction.

### Disk warning before capture

On **Start Capture**, if free space < 50% of total capacity:

> "Only X GB free of Y GB (Z%). Clear old session files or proceed anyway?"

**Clear sessions then start** / **Start anyway** / **Cancel**. Not a hard block.

### Disk warning during CAPTURING

Non-blocking banner added to `/api/status` if free space drops below 50% (polled
every 30 s in the existing stall-detection background task).

### Network share transfer after extraction

After each clip extracts, move to configured destination (NFS/SMB, assumed mounted).
- On success: local copy removed.
- On failure: local file kept, warning in sidecar. Remaining clips continue.
- Local file not removed until destination write confirmed complete.
- Config unset = skip transfer.


## P4 — Testing

### Shutdown tests ✅ DONE (`tests/test_shutdown.py`)
- `test_shutdown_exits_within_7s_with_ws_client_connected` — verifies ws_server hang fix
- `test_session_file_has_valid_moov_after_shutdown` — verifies pipe-drain + moov write

### Still wanted
- Assert no orphaned ffmpeg process after clean shutdown
- Assert ALSA device released after clean shutdown
- Assert session file growing at plausible bitrate 5 s after CAPTURING start
- Sidecar read/write/atomic-update unit tests (no hardware)
- FINALIZING recovery: sidecar with pending extractions + stub MP4 → correct outputs
- Unexpected ffmpeg exit during CAPTURING → server transitions to INDEX, sidecar
  records cause
