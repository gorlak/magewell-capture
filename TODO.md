# TODO

## P3 — Disk space, file management, and output configuration

### Configuration
Output directory goes in a config file (format TBD — likely
`~/.config/magewell-capture/config.toml`). CLI `--output-dir` flag already
exists and takes precedence.

### INDEX page: disk status ✅
Disk bar showing hours remaining (HEVC @20 Mbps) and free/total. Turns red
below 4 h. DELETE endpoints and UI already wired.

### Disk warning during CAPTURING ✅
Low-disk warning (< 4 h remaining) added to the stall-detector background
task; surfaces as a warnings banner in the CAPTURING UI.

### Network share transfer after extraction
After each clip extracts, optionally move to a configured destination
(NFS/SMB, assumed mounted). On success: local copy removed. On failure:
local file kept, warning in meta. Config unset = skip transfer.


## P4 — Tests still wanted

- Assert no orphaned ffmpeg process after clean shutdown
- Assert ALSA device released after clean shutdown
- Stall detector ✅ (`tests/test_monitor.py`) — zero-byte settle, stall, low disk, growing file
- Meta read/write/atomic-update unit tests (no hardware)
- FINALIZING recovery: meta with pending extractions + stub MP4 → correct outputs
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
