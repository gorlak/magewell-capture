# TODO

## P3 — Disk space, file management, and output configuration

### Configuration ✅
Two gitignored repo-local directories: `sessions/` (scratch, auto-created) and
`storage/` (transfer destination — create or symlink to your target). Override
with `--storage-dir DIR` at the command line.

### INDEX page: disk status ✅
Disk bar showing hours remaining (HEVC @20 Mbps) and free/total. Turns red
below 4 h. DELETE endpoints and UI already wired.

### Disk warning during CAPTURING ✅
Low-disk warning (< 4 h remaining) added to the stall-detector background
task; surfaces as a warnings banner in the CAPTURING UI.

### Network share transfer ✅
Manual transfer via the `/view` page Transfer button. `--storage-dir DIR`
flag sets the destination (default: `storage/` symlink in repo root). Uses
`rsync --inplace` (300 s timeout) — `--inplace` avoids the temp-file-then-rename
that CIFS mounts block.
Progress (pct, live MB/s) and completion stats (size, elapsed, avg bandwidth)
are shown and persist on the view page. Local copy is never removed by transfer
— cleanup is via the delete UI on the index page.


## P5 — Evaluate bitrate and recording disk size

Current ffmpeg command uses hardcoded HEVC settings. Need to capture some real
content and assess: are recordings the right size? Is quality sufficient? Is the
disk-hours estimate (@ 20 Mbps) accurate for real output, or does the actual
bitrate differ enough to matter?

- Capture a representative session (mix of motion and static content)
- Check actual output bitrate via `ffprobe`
- Compare to the 20 Mbps constant used in the disk-hours estimate
- Consider whether a configurable bitrate target belongs in `config.toml`


## P4 — Tests still wanted

- Assert no orphaned ffmpeg process after clean shutdown ✅ (implicit via moov check — `test_virtual_device.py`)
- Assert ALSA device released after clean shutdown (hardware-only; skip for now)
- Stall detector ✅ (`tests/test_monitor.py`) — zero-byte settle, stall, low disk, growing file
- Meta read/write/atomic-update unit tests ✅ (`tests/test_meta.py`)
- FINALIZING recovery: stub MP4 + extraction ✅ (`tests/test_meta.py`)
- Unexpected ffmpeg exit ✅ (`tests/test_monitor.py`) — transitions to INDEX, meta written,
  warning recorded, background tasks cancelled


