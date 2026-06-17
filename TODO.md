# TODO

## P3 — Disk space, file management, and output configuration

### Configuration ✅
Repo-local `config.toml` (gitignored). Single key: `transfer_dest`.
If absent, recordings copy to `~/Downloads`. Sessions scratch dir: `./sessions/`
(gitignored). See `config.toml.TEMPLATE` to get started.

### INDEX page: disk status ✅
Disk bar showing hours remaining (HEVC @20 Mbps) and free/total. Turns red
below 4 h. DELETE endpoints and UI already wired.

### Disk warning during CAPTURING ✅
Low-disk warning (< 4 h remaining) added to the stall-detector background
task; surfaces as a warnings banner in the CAPTURING UI.

### Network share transfer ✅
Manual transfer via the `/view` page Transfer button. `--transfer-dest DIR`
flag (or `config.toml`) sets the destination. Uses `rsync --inplace` (300 s
timeout) — `--inplace` avoids the temp-file-then-rename that CIFS mounts block.
Progress (pct, live MB/s) and completion stats (size, elapsed, avg bandwidth)
are shown and persist on the view page. Local copy is never removed by transfer
— cleanup is via the delete UI on the index page.


## P4 — Tests still wanted

- Assert no orphaned ffmpeg process after clean shutdown ✅ (implicit via moov check — `test_virtual_device.py`)
- Assert ALSA device released after clean shutdown (hardware-only; skip for now)
- Stall detector ✅ (`tests/test_monitor.py`) — zero-byte settle, stall, low disk, growing file
- Meta read/write/atomic-update unit tests ✅ (`tests/test_meta.py`)
- FINALIZING recovery: stub MP4 + extraction ✅ (`tests/test_meta.py`)
- Unexpected ffmpeg exit ✅ (`tests/test_monitor.py`) — transitions to INDEX, meta written,
  warning recorded, background tasks cancelled


## Optional

### Resume extraction from pending meta

When FINALIZING crashes or is interrupted, the session meta file may contain
extractions with `"status": "pending"`. On INDEX load, any session whose `.json`
has pending extractions and whose `.mp4` still exists could show a
**Resume extraction** action that re-runs only the pending segments.

Requires a `/api/resume/<session-name>` endpoint that reads the pending
extractions from the meta file and runs them through the existing
`_run_finalization` path. The INDEX listing already has `has_meta` — the
UI hook is there; just needs wiring up.
