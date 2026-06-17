# Magewell Capture — Decisions & Findings

> Originally `handoff.md` (the pre-investigation design brief). Renamed and
> amended on 2026-05-21 after hands-on investigation on the capture box itself.
> This is the living record of what we found and what we decided, and why.

---

## What this project is

A headless HDMI capture + archival appliance. It captures from a **Magewell USB
Capture HDMI Gen2** into NVENC-encoded files, with a network monitoring side to
come later.

**Usage reality (drives the design):**
- **≈99% Apple TV**, possibly **50 Hz**.
- **Occasional VHS/DVD archival** → SD, **interlaced**, possibly **PAL/50 Hz**.

So source-driven detection matters mostly for **frame rate + interlacing** on the
SD/PAL edge cases, not resolution (the Apple TV output is consistently 1080p).

---

## Hardware (unchanged from brief)

ASUS Maximus VI Impact (Z87, Haswell, DDR3) · NVIDIA T400 (Turing, 30W) ·
Magewell USB Capture HDMI Gen2 (UVC+UAC, USB 3.0) · Intel I217-V GbE · ORICO
512GB M.2 SATA · headless, SSH only.

## Software (as verified on the box)

- LMDE 7 / Debian 13 Trixie, kernel 6.12
- NVIDIA driver **595.71.05**, CUDA 13.2 — NVENC (h264/hevc/av1) **confirmed**
- ffmpeg **7.1.4** (Debian build, NVENC enabled) · v4l-utils **1.30.1** · Python **3.13.5**
- glibc **2.41**, libstdc++ GLIBCXX_3.4.33 · g++/gcc present
- `pipx` (apt) → **uv** installed via `pipx install uv`

---

## Device reality (corrects the original brief's assumptions)

The brief assumed a DV-timings capture card emitting nv12 with V4L2 source
detection. The actual device is different:

| Brief assumed | Reality on this unit |
|---|---|
| DV-timings source detection | **UVC device** — `--query-dv-timings` → "Inappropriate ioctl". DV-timings plan does not apply. |
| nv12 native, "no conversion" | **YUYV 4:2:2 only.** ffmpeg must convert YUYV→nv12 for NVENC (also 4:2:2→4:2:0). Conversion is unavoidable and fine. |
| Device reports source format | **It is a scaler** — advertises a fixed ladder 640×360…2048×1080 and scales input to the chosen output. V4L2 format list does **not** reveal true input res. |
| — | True input signal (res/rate/interlace/lock) is only exposed via the **proprietary UVC extension unit → Magewell SDK**. |
| — | **UVC frame-rate ladder = 60/59.94/50/30/29.97/25/15 — no 24/23.976.** 24p film via Apple TV "Match Frame Rate" is frame-rate-converted by the device *before* USB; **clean native 24p capture is impossible** with this hardware. Lever: leave Apple TV Match-Frame-Rate **off** → constant 60. |

Other facts: ALSA capture device is `hw:CARD=HDMI,DEV=0`; `/dev/video0` = capture,
`/dev/video1` = UVC metadata; USB link 5000M (USB 3.0); uvcvideo + snd-usb-audio
bound. Verified live by grabbing 640×360 and 1920×1080 frames of the connected
Apple TV/ABC feed — 1080p passes through 1:1 (sharp on-screen graphics).

---

## Architecture decisions

### 1. Source-driven detection via the Magewell SDK — as a *detector only*
Read input signal status before each capture via the unified MWCapture SDK
(generic API, works for all Magewell devices):
`MWCaptureInitInstance → MWRefreshDevice → MWGetDevicePath → MWOpenChannelByPath
→ MWGetVideoSignalStatus → MWCloseChannel`.
`fps = 10_000_000 / dwFrameDuration`; fields used: `state, cx, cy, bInterlaced,
dwFrameDuration`. The SDK is **only** a detector — all capture/encode stays in
**ffmpeg** (preserves single-NVENC, ffmpeg-only-capture). Pattern validated
against `ReproNim/reprostim`, a real Linux Magewell→ffmpeg pipeline.

**Opportunistic + fallback:** if the SDK reports `locked`, drive ffmpeg from
detected res/fps/interlace; otherwise fall back to **1920×1080@60**.

### 2. Binding: ctypes (not pybind11/.pyd)
Tiny surface (7 functions, 1 struct) → ctypes needs no compile step and is
Python-version-independent. Promote to pybind11 only if the surface grows.

### 3. The native `.so`
Build `libMWCapture.so` natively from the SDK static `.a` (**3.3.1.1515**) via
Magewell's `gen_shared.sh` linkage (`CLIB="-lpthread -ldl -ludev -lasound
-lv4l2"`) so it records proper `DT_NEEDED` and loads with a **plain
`ctypes.CDLL`** (no preload).
- The prebuilt 3.3.1.0 `.so` lacked those NEEDED entries (needed an
  `RTLD_GLOBAL` preload of asound/udev/v4l2) — building from the `.a` fixes it.
- No `-dev` packages needed: point the linker at the system runtime libs via
  `LIBRARY_PATH` symlinks (`libasound.so.2`, etc.). g++ already present.
- Compatibility verified: lib needs GLIBC 2.17 / GLIBCXX 3.4 / CXXABI 1.3
  (≤ system). Runtime deps `libasound2`/`libudev1`/`libv4l-0` (all present).
  Validated: ctypes load + `MWGetChannelCount() == 1`.
- SDK USB path rides on V4L2 + udev (no libusb). SDK probe and ffmpeg both touch
  V4L2 but **sequentially** (probe → close channel → launch ffmpeg) → no conflict.

### 4. License & vendoring
The SDK header notice is a permissive **BSD/MIT-style** grant scoped to the
**headers + library** (not the SDK as a whole): use/publish/distribute/sublicense
permitted provided the **copyright notice is retained** and the **AS-IS
disclaimer is included**. → Vendoring the `.a` + headers is allowed (even
public), with attribution. The verbatim notice is kept as `LICENSE.txt` beside
the vendored SDK (`packages/magewell/vendor/mwcapture-sdk-<ver>/LICENSE.txt`) —
the single source of truth, version-stamped so it can't drift.
Vendored artifacts are **version-stamped** (`vendor/mwcapture-sdk-3.3.1.1515/`)
with a `SOURCE.txt` (URL + SHA256 + date). New SDK release = new sibling dir +
bump pointer; old retained for reproducibility. The **built `.so` is gitignored**
(regenerated per box via `build_lib.sh`).

### 5. Repo / packaging
- Repo **`magewell-capture`** = durable source of truth; built artifacts
  (`.so`, wheels, `.venv`) are reproducible + gitignored.
- **uv workspace** (uv via `pipx install uv`); `.venv` at repo root; `uv sync`
  installs all packages editable.
- Package **`magewell`** = generic SDK wrapper (ctypes + bundled `.so`).
  Device-specific features, if ever needed, go in a `magewell.usb` **submodule**
  rather than renaming the package.
- Capture logic = **loose, editable `scripts/capture.py`**, run via the venv
  (shebang to `.venv/bin/python`; symlink into `~/.local/bin` for "ssh in and run").
- Future packages (e.g. `packages/streammonitor` for Phase 2) join as workspace
  members.

---

## Capture pipeline

### Encode settings
- Video in: V4L2 `/dev/video0`, `yuyv422`, detected (or fallback) W×H + fps → convert to nv12.
- Audio in: **direct ALSA** `hw:CARD=HDMI,DEV=0` (not PipeWire). PipeWire is
  running → may need `pactl suspend-source` on the Magewell source before capture.
- Encode: `hevc_nvenc -preset p6 -rc vbr -cq 21 -b:v 0 -maxrate 80M -bufsize 160M
  -profile:v main10 -spatial-aq 1`, GOP = 2×fps.
- Audio: `aac -b:a 192k -af aresample=async=1000`.
- Interlaced SD: detect via SDK `bInterlaced`; deinterlace policy (bwdif/yadif vs
  preserve-interlaced) is an **open question** for the archival path.

### Codec choice: HEVC over H.264
Switched from `h264_nvenc` (Phase 1 testing) to `hevc_nvenc` for ~20% bitrate
savings at equivalent quality. Main10 profile chosen over Main — it's a superset,
universally supported, and gives the encoder more internal precision even with
8-bit input. **Temporal AQ is not supported** on the T400 (Turing) for HEVC —
only spatial AQ is used. Tested: 1080p59.94 encodes at ~20 Mbps CQ21 (~10 GB/hr),
~0.999x realtime on the T400.

### Audio: USB hardware delivers stereo only

The Magewell USB Capture HDMI Gen2 exposes audio to the host via USB Audio
Class (UAC), which is hardcapped at **2 channels, S16_LE, 48 kHz**:

```
$ cat /proc/asound/HDMI/stream0
  Interface 3 / Altset 1
    Format: S16_LE  Channels: 2  Rates: 48000  Channel map: FL FR
```

The Magewell's **EDID advertises 8-channel LPCM** — this is what it tells
the source it can *receive* over HDMI, and it is accurate: the HDMI receiver
inside the device can accept multichannel audio. But the USB audio path to the
host discards everything above 2 channels. The SDK's `AudioSignal.num_channels`
reflects the HDMI signal metadata (e.g. 6 for 5.1), **not** what ALSA delivers.
Do not use `num_channels` to set ffmpeg's `-ac`.

**Current code** is therefore stereo-only: single AAC 192k track, no
multichannel branch. The LPCM/compressed check in `probe_signal()` remains as
a warning guard in case the source changes audio format.

#### Restoring multichannel support (PCIe card path)

A PCIe capture card (e.g. Magewell Pro Capture HDMI 4K Plus) exposes
multichannel ALSA — the channel count the SDK reports matches what ALSA
delivers. To re-enable:

1. **`probe_signal()`** — return `audio_channels` from SDK:
   ```python
   audio_channels = max(2, audio.num_channels)  # valid for PCIe; NOT for USB
   return sig.width, sig.height, sig.fps, sig.interlaced, audio_channels
   ```

2. **`build_input_args()`** — accept and pass `audio_channels`:
   ```python
   "-ac", str(audio_channels),   # instead of hardcoded "2"
   ```

3. **`_mc_filter_complex(n_stereo_copies)`** — splits `[1:a]` into
   multichannel + N stereo downmix copies:
   ```python
   def _mc_filter_complex(n_stereo_copies):
       n = n_stereo_copies + 1
       splits = "".join(f"[a{i}]" for i in range(n))
       chains = [f"[1:a]asplit={n}{splits}"]
       chains.append("[a0]aresample=async=1000[mc]")
       stereo_labels = []
       for i in range(1, n):
           sl = f"[s{i-1}]"
           stereo_labels.append(sl)
           chains.append(f"[a{i}]aresample=async=1000,aformat=channel_layouts=stereo{sl}")
       return ";".join(chains), "[mc]", stereo_labels
   ```

4. **`_eac3_bitrate(audio_channels)`**:
   ```python
   def _eac3_bitrate(audio_channels):
       return f"{max(640, audio_channels * 128)}k"
   ```

5. **Command builders** — branch on `audio_channels > 2`:
   - `build_capture_cmd`: 1 stereo copy → EAC-3 multichannel + AAC stereo
   - `build_monitor_cmd`: 2 stereo copies → EAC-3 + AAC stereo (file) + AAC
     stereo (preview pipe)

   ```python
   if audio_channels > 2:
       fc, mc, (s0,) = _mc_filter_complex(1)   # capture
       fc, mc, (s0, s1) = _mc_filter_complex(2) # monitor
       cmd += ["-filter_complex", fc]
       cmd += ["-c:a:0", "eac3", "-b:a:0", _eac3_bitrate(audio_channels),
               "-c:a:1", "aac", "-b:a:1", "192k"]
       cmd += ["-map", "0:v", "-map", mc, "-map", s0]
   else:
       cmd += build_encode_args(fps)   # single AAC track
   ```

#### IEC 61937 / compressed bitstream path (spdif demuxer)

If the source sends compressed audio (AC-3, EAC-3, TrueHD) rather than LPCM,
ALSA presents the IEC 61937 bitstream as fake PCM bytes. ffmpeg's ALSA input
treats them as linear PCM → garbled audio. To handle this:

- Start `arecord` writing raw bytes to a named FIFO.
- Read the FIFO with `-f spdif` in ffmpeg, which decodes the IEC 61937 framing.
- The FIFO bridges ALSA-aware `arecord` and the byte-stream spdif demuxer.
- Carrier params: AC-3 → 2ch 48 kHz; EAC-3 → 2ch 192 kHz; TrueHD HBR → 8ch
  192 kHz. Set `arecord -c / -r` accordingly from SDK `AudioSignal.sample_rate`
  and `num_channels`.

**This path was removed** because the current source (Apple TV + Magewell EDID
restricted to LPCM) reliably delivers LPCM. The spdif path has worse A/V sync
than direct ALSA: the spdif demuxer counts bytes from t=0 rather than using
hardware timestamps, and aligning it with V4L2 wall-clock video requires
`aresample=async=1000` for drift correction only (not for initial offset).

### ALSA timestamps
Do **not** use `-use_wallclock_as_timestamps` on the ALSA audio input. ALSA
provides accurate timestamps from the hardware sample counter; overriding with
wall clock causes mis-timestamped bursts that `aresample=async` turns into silence
gaps (audio plays a few samples then drops out). Wallclock timestamps are correct
for the V4L2 video input only.

### Quality settings rationale
hevc_nvenc (better compression, broad support) · VBR CQ21 (content-adaptive,
transparent) · preset p6 · maxrate 80M ceiling · **input yuyv422 → nv12**
(conversion required) · spatial AQ on · AAC 192k · aresample async.

---

## Phased plan

- **Phase 1:** `magewell` ctypes binding → `capture.py` — **done**.
- **Phase 2:** monitored capture with browser-based preview and record control — see below.

---

## Phase 2: Monitored capture (`scripts/monitor.py`)

### Overview
A separate script from `capture.py` (which is preserved for simple one-shot
captures and diagnostics). `monitor.py` provides continuous capture with a
browser-based preview and record in/out control.

### Architecture: always-capture, mark-and-extract

1. **ffmpeg runs continuously** from start with two outputs (dual encode):
   - **Output 1:** MP4 file on disk with `faststart` (the recording, ~16 Mbps)
   - **Output 2:** fragmented MP4 to pipe (`frag_keyframe+empty_moov`) for
     WebSocket+MSE preview in the browser
   Uses two NVENC sessions (T400 supports 3 concurrent). The tee muxer was
   rejected because it doesn't forward codec extradata (produces empty `hvcC`
   in the fMP4 init segment, which MSE rejects — see "Findings" below).

2. **Python async server** (`websockets` library) on `:8090` (dev) / `:80` (service), serving:
   - WebSocket stream: fMP4 data from ffmpeg pipe → broadcast to browser clients
   - Static web UI (the monitor page)
   - JSON API for record control (`/api/mark-in?t=N`, `/api/mark-out?t=N`,
     `/api/status`)

3. **Web UI** (single HTML page, no build step):
   - HEVC video via **WebSocket + Media Source Extensions** (MSE). The browser
     receives fMP4 fragments over WebSocket and feeds them to a `<video>`
     element via MSE's SourceBuffer API. ~3s latency (one GOP).
   - **RECORD / STOP button** — sends `GET /api/mark-in?t=<video.currentTime>`
     (stream time from the player, so cuts match what the user saw on screen)
   - **Status badge**: STANDBY (grey) or RECORDING (red, pulsing)
   - **Timecode display** showing `video.currentTime` (stream position)
   - **Mute/Unmute button** (autoplay requires muted; user clicks to unmute)
   - **Spacebar** keyboard shortcut for record toggle
   - Safari is the primary target; Firefox HEVC+MSE is unreliable

4. **In/out point tracking:** uses `video.currentTime` from the browser — the
   stream position of the frame currently displayed. This means cuts match
   what the user saw regardless of preview latency. The server stores
   `(in_time, out_time)` pairs in stream seconds.

5. **Post-capture segment extraction:** on SIGINT (Ctrl-C), ffmpeg shuts down
   gracefully, then Python extracts each marked segment with
   `ffmpeg -ss <in> -to <out> -c copy <output>` — pure stream copy, no
   re-encode, nearly instant. The continuous session MP4 is retained.

### Findings during implementation

- **ffmpeg tee muxer loses codec extradata.** The tee muxer doesn't forward
  HEVC VPS/SPS/PPS to slave muxers. The fMP4 init segment has an empty
  `hvcC` box (8 bytes, header only). MSE requires a populated `hvcC` to
  initialize the decoder. Direct output (no tee) works correctly. Workaround:
  dual ffmpeg outputs (2 NVENC sessions) instead of tee.
- **`empty_moov` is required for fMP4/MSE** — without it, ffmpeg writes a
  regular moov with full sample tables (no `mvex`), which MSE rejects.
  With `empty_moov`, the moov has proper fMP4 structure (empty stbl, mvex
  with trex). For direct output (no tee), `empty_moov` still populates the
  `hvcC` because NVENC provides extradata during encoder initialization.
- **HEVC MSE codec strings:** Safari uses `hev1.2.4.L120.B0` (Main 10,
  Level 4.0) for HEVC in fMP4. ffmpeg outputs `hev1` format (parameter sets
  in-band), not `hvc1`. Using the wrong codec string (`hvc1` when stream is
  `hev1`) causes silent decode failure.
- **Hand-rolled WebSocket failed in browsers.** A raw asyncio HTTP+WebSocket
  server with correct RFC 6455 implementation (verified: accept key, response
  format, hex dump) was rejected by both Safari ("cannot parse response") and
  Firefox (code 1006) despite passing in-process tests. The `websockets`
  library (v16) works immediately. Root cause not fully determined — likely
  subtle HTTP/1.1 framing or buffering behavior that browsers are strict about.
- **Double SIGINT corrupts MP4 faststart.** When the user presses Ctrl-C,
  ffmpeg receives SIGINT from the terminal process group. If Python also sends
  SIGINT, the second signal interrupts ffmpeg during the moov-to-front second
  pass, producing a file with no moov. Fix: `capture.py` ignores SIGINT
  (lets ffmpeg handle it); `monitor.py` tracks which signal was received and
  only forwards SIGTERM.
- **stop_ffmpeg stdout drain race.** The original `stop_ffmpeg` cancelled
  `_pipe_task`, awaited it, *then* created a `drain_task` for stdout, then
  sent SIGINT. The window between pipe_task cancel and drain_task start allowed
  ffmpeg's stdout pipe to fill up, potentially blocking SIGINT handling. A
  stalled WebSocket client could also block `broadcaster.feed()`, holding
  `_pipe_task` indefinitely and preventing SIGINT from ever being sent. Fix:
  set `_stopping_ffmpeg = True` first (makes `_read_pipe` drain without
  broadcasting — no gap), send SIGINT immediately, cancel `_pipe_task` only
  after `proc.wait()` completes. `broadcaster.feed()` also gains a 2 s per-
  client send timeout to bound how long a stalled client can block the loop.
- **`_log_stderr` LimitOverrunError on long captures.** ffmpeg writes progress
  updates to stderr using `\r` (carriage return) to overwrite the same terminal
  line. asyncio's `readline()` waits for `\n` (line feed), so all the `\r`-
  terminated progress updates accumulate in the 64 KB StreamReader buffer. After
  ~10 minutes of capture (~640 updates × ~100 bytes), the buffer overflows and
  `readline()` raises `LimitOverrunError`. `_log_stderr` only caught
  `CancelledError`, so the task died. With nobody reading stderr, the stderr
  pipe filled and ffmpeg blocked writing its next progress update — unable to
  process SIGINT and unable to exit. Fix: catch `LimitOverrunError` inside
  `_log_stderr`, drain stderr in 64 KB chunks until a `\n` or EOF is found,
  then resume `readline()` for real log lines.
- **fMP4 fragment latency:** `frag_keyframe` with a 2-second GOP produces
  ~3s preview latency (one GOP buffered before first fragment emits, plus
  network/decode). Acceptable for monitoring. Could be reduced by shortening
  the preview output's GOP independently.
- **`-tag:v hvc1` required for iOS playback.** ffmpeg 7.1 defaults to the
  `hev1` codec tag for HEVC in MP4. `hev1` stores decoder configuration as
  in-band parameter sets; `hvc1` stores it in the `hvcC` box. iOS
  AVFoundation (Safari's native `<video>` element) requires `hvc1`; files
  tagged `hev1` fail to play on iOS with a `MEDIA_ERR_DECODE` error. Fix:
  add `-tag:v hvc1` to `_video_encode_args`. Stream copy (`-c copy`) in
  `build_extract_cmd` inherits the tag from the session file automatically —
  no `-tag:v` needed on the extract side.
- **`stop_ffmpeg` D-state hang on shutdown.** After `asyncio.wait_for(proc.wait(),
  timeout=15s)` times out, `proc.kill()` (SIGKILL) is sent and then
  `await proc.wait()` is called. If ffmpeg is stuck in an unkillable kernel
  call — V4L2 or ALSA `close()` in D-state (uninterruptible sleep) — SIGKILL
  cannot wake it and the bare `await proc.wait()` hangs forever. The asyncio
  event loop stays live (status polls still work), but finalization never
  advances. Fix: wrap the post-SIGKILL `proc.wait()` in a 10-second
  `asyncio.wait_for`; on timeout, log and proceed. The orphaned ffmpeg process
  clears when the kernel call eventually unblocks or the device is reset.

### Why this design (alternatives rejected)

- **Restart ffmpeg on record start/stop:** brittle, causes preview glitches,
  risk of lost frames at transitions.
- **HLS instead of WebSocket+MSE:** 6+ seconds latency due to segment
  buffering and playlist polling. Too slow for monitoring fast-cut content.
- **RTSP:** low latency (~0.5s) but no browser support — would require VLC.
- **ffmpeg tee muxer:** doesn't forward codec extradata to slave muxers,
  producing invalid fMP4 for MSE. Dual encode (2 NVENC sessions) works.
- **Hand-rolled WebSocket server:** browsers reject it despite correct
  protocol implementation. The `websockets` library handles edge cases.
- **PyAV / libav bindings:** unnecessary complexity for this pipeline.

### Script separation
`capture.py` — simple one-shot capture, no server, no preview. Kept for quick
testing and diagnostics (e.g. `capture.py --duration 5`).
`monitor.py` — monitored capture with browser preview and record control.
Both share `capture_shared.py` (ffmpeg command builders, signal probe, path
generation).

### Extraction seeking: input-mode

`build_extract_cmd` places `-ss` **before** `-i` (input-mode seeking).

For a muxed MP4, input-mode seeking finds the keyframe at or before the target
time and seeks the **entire file** to that position. Both audio and video
depart from the same keyframe → perfect A/V sync. The clip has a brief
pre-roll (≤ one GOP, ≤ 1 s) before the exact mark-in point.

**Output-mode was tried and rejected:** placing `-ss` after `-i` causes ffmpeg
to start audio at the exact mark-in time but video at the first keyframe AT OR
AFTER mark-in (up to one GOP later). This produces a persistent audio-ahead-
of-video offset equal to the keyframe gap for the full clip duration — the
opposite of what was originally assumed (we incorrectly believed input-mode
made audio late; it does not — it seeks both streams together).

**`-reset_timestamps 1` was tried and rejected:** it resets each stream
**independently** to PTS=0, causing a sustained A/V offset equal to the gap
between audio and video start PTSes (up to one GOP). Removed.

**GOP size:** `round(fps)` (1 s at 60 fps) bounds the pre-roll window. The
original 2-second GOP (`round(fps * 2)`) doubled it unnecessarily.

The 0.15 s end pad (`end + 0.15`) avoids dropping the last audio packet when
the cut falls on a packet boundary.

### Shutdown: HTTP CLOSE-WAIT hang

When a browser stops reading an MP4 mid-stream (tab closed, navigation, etc.),
the TCP connection enters CLOSE-WAIT with the kernel send buffer full. In this
state, `asyncio.StreamWriter.close()` schedules `connection_lost()` only once
the write buffer drains — which never happens. `writer.wait_closed()` blocks
indefinitely.

Fix: call `writer.transport.abort()` between `close()` and `wait_closed()`.
`abort()` calls `_force_close()` → `_call_connection_lost()` immediately,
regardless of buffer state (sends a TCP RST). `wait_closed()` then returns in
the next event loop iteration.

The full shutdown sequence requires all four pieces to work together:
1. `asyncio.timeout(3.0)` around `ws_server.wait_closed()` — handles stubborn
   WebSocket clients that never send a Close frame.
2. `close_timeout=1` on the websockets server — limits per-client WS close
   handshake time.
3. Task cancellation sweep in `async_main` — cancels any still-running
   `_handle()` coroutines after the server stops accepting.
4. `transport.abort()` in `_handle()`'s finally block — drains the stuck HTTP
   send buffer so `wait_closed()` returns.

Items 3 and 4 are coupled: the sweep (3) cancels the stuck `_handle()` task,
propagating `CancelledError` to its finally block where `abort()` (4) fires.

### Transfer workflow: manual, view-page-triggered

Transfer is not automatic after extraction. The user proofs the recording on
the `/view` page first, then clicks **Transfer** to copy it to `storage_dir`.

**Why manual:** auto-transfer after extraction would send files the user hasn't
reviewed yet, and gives no opportunity to skip a bad take. The view page is the
natural proof step; transfer is the intentional archival act.

**rsync over cp:** `rsync` performs block-level checksum verification during
transfer (unlike `cp`). `--inplace` is required for CIFS/Samba mounts — rsync's
default temp-file-then-rename fails with `EPERM` on those filesystems.
`--archive` is deliberately not used: it tries to set permissions and ownership,
which also fails on network mounts.

**Local copy kept:** transfer never removes the source file. Cleanup is via the
delete UI on the index page. This prevents accidental data loss if a transfer
succeeds but the destination is later unavailable.

**Progress:** the view page polls `GET /api/recording/<name>/transfer` at 500 ms.
The server polls `dest.stat().st_size` every 500 ms during the rsync subprocess
to compute pct and instantaneous MB/s. On completion, size, elapsed, and avg
bandwidth are recorded and displayed permanently on the view page.

**Storage configuration:** no config file. The transfer destination is
`<repo>/storage/` by default — create that directory or symlink it to any target
(e.g. `ln -s /mnt/nas storage`). Override at runtime with `--storage-dir DIR`.
If `storage/` does not exist when Transfer is clicked, the API returns a 412 with
a clear setup message. The old `config.toml` mechanism has been removed.

### Lifecycle management: Makefile + systemd service

`make install` writes two files as root and starts a systemd service:
- `/etc/udev/rules.d/70-magewell.rules` — device node access
- `/etc/systemd/system/magewell-capture.service` — unit file with `User=<you>`,
  absolute paths baked in, `AmbientCapabilities=CAP_NET_BIND_SERVICE` for port 80

`make install` is a no-op if the service is already running — bouncing a live
capture must be intentional (`make restart`). `make run` stops the service if
running and launches interactively for dev iteration.

Port 80 requires `CAP_NET_BIND_SERVICE`; only the service process gets it via
`AmbientCapabilities`. Dev mode (`make run`) uses port 8090 and needs no
capability. The WebSocket port is always HTTP_port + 1 (81 in service mode,
8091 in dev). The browser computes this as `(parseInt(location.port) || 80) + 1`
— `location.port` is empty string on port 80 (browsers omit default ports).

### Session lifecycle: no separate REPORT state

The original design had a four-state cycle: INDEX → CAPTURING → FINALIZING →
REPORT → INDEX, where REPORT was a dedicated page showing extraction results.

This was simplified: FINALIZING transitions directly to INDEX. The universal
`/view?file=<name>` viewer serves both sessions and recordings, loading the
associated `.json` metadata asynchronously if present and displaying it as a
flat key/value table. Reasoning:
- A dedicated REPORT page is ephemeral (lost on restart) and duplicates what
  INDEX already shows from the meta file.
- The `/view` page works at any time — during FINALIZING, after restart, for
  old recordings — with no state dependencies.
- "File first" model: the video file is the primary entity; metadata enriches
  it but is never required. `/view` always shows the player; the metadata table
  appears only if the `.json` exists.

## Known issues / watch-for

- **SDK device access needs a udev rule (USB node + hidraw).** The SDK opens the
  raw USB node (`/dev/bus/usb/BBB/DDD`) to open the channel, and uses the device's
  HID interface (`/dev/hidrawN`) for control/status transfers
  (`MWGetVideoSignalStatus`). Both are root-only by default: without USB access
  `MWOpenChannelByPath` fails (`EACCES`); with USB but **not** hidraw the channel
  opens but every SDK call returns `MW_FAILED`. Both are granted by
  `packages/magewell/udev/70-magewell.rules` (plugdev rw on idVendor 2935),
  applied via `sudo ./system-setup.sh`. ffmpeg/V4L2 capture is unaffected
  (`/dev/videoN`, group `video`).
- **SDK version must match the device's USB interface layout.** SDK 3.3.1.1313
  (from the ReproNim/reprostim mirror) failed with `USBDEVFS_CLAIMINTERFACE`
  / `USBDEVFS_SUBMITURB` returning `ENOENT` — it targeted a USB
  interface/endpoint that this Gen2 unit (fw `0x289f`) doesn't expose. Confirmed
  via `strace`; failed identically even as root. The official **3.3.1.1515** from
  magewell.com resolved it. Record the working SDK version; if upgrading the
  device firmware or SDK, re-test.
- **ALSA buffer xruns** (PipeWire contention). PipeWire is running and
  occasionally contests the ALSA device, producing `ALSA buffer xrun` warnings
  during capture. The `aresample=async=1000` filter absorbs these without audible
  artifacts. If they become frequent on long captures, suspend PipeWire's hold
  before capture: `pactl suspend-source <magewell_source> 1`. Or remove
  PipeWire entirely (headless box, no other audio needs).
- **No native 24p capture** (device limitation) — manage via Apple TV frame-rate setting.
- Power instability in TV room (slow POST) → consider UPS / line conditioning.
- Use rear Intel xHCI USB 3.0 ports.
- DV-timings detection N/A (UVC scaler) — detection is via the SDK.

## Capture test results (2026-05-21)

### H.264 (Phase 1 initial test)
- Input detected: `1920x1080p59.946`, RGB, limited quant/sat, 2D progressive.
- Audio: 48000 Hz / 16-bit LPCM, stereo.
- Output: h264 High (NVENC CQ21 p6), yuv420p, ~25 Mbps; AAC LC 192k; `faststart`.
- Encode speed: ~0.97x real-time on the T400.
- File: 15.1 MB / 5s.
- 3 initial frames dropped (startup transient), 2 ALSA xruns (PipeWire, absorbed).

### HEVC (current)
- 5-second test: HEVC Main 10, ~20 Mbps CQ21, 14.2 MB (vs 15.1 MB H.264). No xruns.
- **Audio fix applied:** removed `-use_wallclock_as_timestamps` from ALSA input —
  was causing corrupted audio (a few samples every ~200ms, rest silence). See
  "ALSA timestamps" above.
- **Temporal AQ removed:** T400 does not support temporal AQ for HEVC
  (`hevc_nvenc` fails at encoder init). Spatial AQ only.
- 60-second test: 3595 frames, 167.1 MB (~20 Mbps avg), steady 0.999x realtime,
  no xruns or errors. Projected ~10 GB/hour.

## Current status (2026-06-17)

**Phase 1 and Phase 2 complete.**

Phase 2 deliverables (in addition to Phase 1):
- `scripts/monitor.py`: continuous HEVC capture with dual-output (session file +
  fMP4 pipe), browser preview via WebSocket+MSE, mark-in/out record control,
  recording extraction on complete. 125 tests passing.
- `scripts/web/index.html`: capture UI — live HEVC preview, record button,
  session/recording file management with disk usage bar. Finalizing page shows
  live extraction progress (%, speed multiplier, ETA, elapsed) via ffmpeg's
  `-progress pipe:1` output.
- `scripts/web/view.html`: recording viewer — playback (iOS-compatible `hvc1`
  tag), error code display, metadata table, manual transfer to `storage_dir`
  with live progress and completion stats.
- `Makefile`: lifecycle management — `make install` (udev + venv + systemd service
  on :80), `make run` (dev mode on :8090), `make restart`, `make clean`,
  `make status`.
- `SECURITY.md`: agentic authorship disclosure, privilege model, network surface,
  architecture FAQ.

---

## Test strategy

Tests are organized into three tiers based on what they require to run.

### Tier 1: Unit tests (CI — GitHub Actions)

Pure logic, no hardware, no kernel modules, no GPU. These run on every push.

- **Struct layout tripwires** (existing) — `ctypes.sizeof` / field offset checks
  against SDK header constants.
- **ffmpeg command builder** — given (width, height, fps, interlaced, output),
  assert the correct argv is produced. Covers: HEVC settings, tee muxer output
  format, HLS options, ALSA timestamps not present on audio input, NTSC
  framerate ratio mapping, fallback values.
- **State machine** — STANDBY → RECORDING → STANDBY transitions, edge cases
  (double mark-in, mark-out without mark-in, etc.).
- **In/out point bookkeeping** — adding points, listing them, timestamps are
  monotonic, pairing logic.
- **Extraction command builder** — given a list of (in, out) pairs and a source
  file, assert correct `ffmpeg -ss -to -c copy` argv for each segment.
- **HTTP API routing** — test request/response for `/api/mark-in`,
  `/api/mark-out`, `/api/status` against the server handler directly (no
  network), using aiohttp test client or similar.

### Tier 2: Virtual device integration (local — any Linux box)

Requires kernel modules (`v4l2loopback`, `snd-aloop`) but **not** the Magewell
or NVIDIA GPU. Uses software encoders (`libx265`/`libx264`) as a fallback.
Tests the actual ffmpeg pipeline end-to-end with virtual devices.

Setup:
```
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="Test"
sudo modprobe snd-aloop index=10
ffmpeg -re -stream_loop -1 -i test/fixtures/test_card.mp4 \
  -f v4l2 -pix_fmt yuyv422 /dev/video10 \
  -f alsa hw:Loopback,0
```

What this covers:
- Full capture pipeline (input → encode → mux → file), just with virtual devices
  and software encode.
- HLS output: segments appear in `/dev/shm`, playlist is valid.
- Segment extraction: in/out → `ffmpeg -c copy` → output files are playable.
- HTTP server: HLS segments are served, API responds correctly.
- End-to-end browser preview (manual — open in Safari/Firefox).

What this does **not** cover:
- NVENC encoding (needs GPU).
- Real USB device timing, xruns, signal detection.

Tests in this tier are marked `@pytest.mark.virtual_device` and skipped if the
loopback devices are not present.

### Tier 3: Hardware tests (capture box only)

Requires the Magewell device, NVIDIA GPU, and a live HDMI signal. Run manually
on the capture box (`pytest -m device`). The existing `requires_device` skip
pattern is already in place.

What this covers:
- SDK signal detection (resolution, fps, interlace, color, audio).
- NVENC encoding at full quality.
- Real ALSA timing and xrun behavior.
- A/V sync on actual captured files.

### Pytest markers and CI configuration

Markers:
- (unmarked) — Tier 1, runs everywhere including CI.
- `@pytest.mark.virtual_device` — Tier 2, skipped without loopback modules.
- `@pytest.mark.device` (existing `requires_device`) — Tier 3, skipped without
  Magewell hardware.

CI runs **Tier 1 only**: `pytest -m "not virtual_device and not device"`.

### CI portability (GitHub Actions now, Codeberg/Woodpecker later)

The CI configuration is kept deliberately portable — no platform-specific
features that can't be trivially replicated elsewhere. The repo may move to
Codeberg (Woodpecker CI, Docker-based YAML config).

Principles:
- **The test command is the entire contract:**
  `uv sync && uv run pytest -m "not virtual_device and not device"`.
  Same command on any CI, any Linux container.
- **No marketplace actions** beyond `actions/checkout`. Install uv via
  `pipx install uv` or `curl`, not `astral-sh/setup-uv`. Woodpecker has no
  action marketplace equivalent.
- **No GitHub-specific API usage** in CI (no `GITHUB_TOKEN`, no
  `github.event` context, no status checks API). If we need CI status badges,
  use the generic endpoint both platforms expose.
- **Base image: any Debian/Ubuntu with Python 3.13, g++, make.** The `.so`
  build (`build_lib.sh`) needs `g++` and the vendored `.a` — no exotic deps.
- **Single CI file at repo root** (`.github/workflows/test.yml` for now;
  `.woodpecker.yml` equivalent is a ~10 line translation when the time comes).

### Test fixtures

A short (~5 second) test card video+audio file at `tests/fixtures/test_card.mp4`
(committed to the repo, small enough at ~1 MB) for Tier 2 virtual device tests.
Content: colour bars + 1kHz tone, 1080p60, HEVC+AAC. Generated once via ffmpeg:
```
ffmpeg -f lavfi -i testsrc2=s=1920x1080:r=60:d=5 \
       -f lavfi -i sine=f=1000:r=48000:d=5 \
       -c:v libx265 -preset fast -crf 28 \
       -c:a aac -b:a 128k \
       tests/fixtures/test_card.mp4
```
