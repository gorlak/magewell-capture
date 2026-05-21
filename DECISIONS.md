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

## Capture pipeline (target ffmpeg behavior)

- Single NVENC encode, file output (Phase 1); later tee to HLS in `/dev/shm` (Phase 2).
- Video in: V4L2 `/dev/video0`, `yuyv422`, detected (or fallback) W×H + fps → convert to nv12.
- Audio in: **direct ALSA** `hw:CARD=HDMI,DEV=0` (not PipeWire). PipeWire is
  running → may need `pactl suspend-source` on the Magewell source before capture.
- Encode: `h264_nvenc -preset p6 -rc vbr -cq 21 -b:v 0 -maxrate 80M -bufsize 160M
  -profile:v high -spatial-aq 1 -temporal-aq 1`, GOP = 2×fps.
- Audio: `aac -b:a 192k -af aresample=async=1000`.
- Interlaced SD: detect via SDK `bInterlaced`; deinterlace policy (bwdif/yadif vs
  preserve-interlaced) is an **open question** for the archival path.

### Quality settings rationale (unchanged except pix_fmt)
h264_nvenc (mature, compatible) · VBR CQ21 (content-adaptive, transparent) ·
preset p6 · maxrate 80M ceiling · **input yuyv422 → nv12** (conversion required,
was assumed unnecessary) · spatial+temporal AQ on · AAC 192k · aresample async.

---

## Phased plan

- **Phase 1:** `magewell` ctypes binding → `capture.py` (detect → ffmpeg →
  timestamped file in `~/captures`). Verify A/V sync + a long capture.
- **Phase 2:** streaming monitor — ffmpeg tee → HLS in `/dev/shm/capture_monitor`;
  separate Python `http.server` on :8090; firewall to LAN.

## Known issues / watch-for

- **SDK device access needs a udev rule (USB node + hidraw).** The SDK opens the
  raw USB node (`/dev/bus/usb/BBB/DDD`) to open the channel, and uses the device's
  HID interface (`/dev/hidrawN`) for control/status transfers
  (`MWGetVideoSignalStatus`). Both are root-only by default: without USB access
  `MWOpenChannelByPath` fails (`EACCES`); with USB but **not** hidraw the channel
  opens but every SDK call returns `MW_FAILED`. Both are granted by
  `packages/magewell/udev/70-magewell.rules` (plugdev rw on idVendor 2935),
  applied via `sudo ./setup.sh`. ffmpeg/V4L2 capture is unaffected
  (`/dev/videoN`, group `video`).
- **No native 24p capture** (device limitation) — manage via Apple TV frame-rate setting.
- PipeWire may claim the Magewell ALSA device → suspend or remove.
- Power instability in TV room (slow POST) → consider UPS / line conditioning.
- Use rear Intel xHCI USB 3.0 ports.
- DV-timings detection N/A (UVC scaler) — detection is via the SDK.

## Current status (2026-05-21)

- Env verified; ffmpeg/NVENC/v4l-utils OK.
- `libMWCapture.so` built natively + validated (plain ctypes load, device seen).
- Repo scaffolded as a uv workspace; SDK vendored (3.3.1.1313) with license;
  `magewell` package skeleton in place.
- **Next:** write `packages/magewell/src/magewell/_sdk.py` — the ctypes binding
  (`MWCAP_VIDEO_SIGNAL_STATUS` from the vendored header, the 7 prototypes,
  `read_signal()`), then a live-signal test against the connected feed.
