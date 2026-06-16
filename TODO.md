# TODO

## P2 — Resume extraction from pending meta

When FINALIZING crashes or is interrupted, the session meta file may contain
extractions with `"status": "pending"`. On INDEX load, any session whose `.json`
has pending extractions and whose `.mp4` still exists should show a
**Resume extraction** action that re-runs only the pending segments.

Requires a `/api/resume?session=<name>` endpoint (or similar) that:
- Validates the session MP4 exists
- Reads the meta file, finds pending extractions
- Transitions to FINALIZING and runs `_run_finalization` for those segments only
- Updates the meta file on each completion

The INDEX page already shows `has_meta` in the file listing — the UI hook
is there; just needs wiring up.


## P3 — Disk space, file management, and output configuration

### Configuration
Output directory goes in a config file (format TBD — likely
`~/.config/magewell-capture/config.toml`). CLI `--output-dir` flag already
exists and takes precedence.

### INDEX page: disk status
**Disk usage bar** — free / total for the output filesystem. Refreshed on load.
The DELETE endpoints are already implemented (`/api/session/<name>`,
`/api/sessions`, `/api/recording/<name>`, `/api/recordings`); the INDEX page
doesn't surface them yet.

### Disk warning before capture
On **Start Capture**, if free space < 50% of total capacity, show a
non-blocking warning before proceeding.

### Disk warning during CAPTURING
Banner added to `/api/status` if free space drops below 50%. Can piggyback on
the existing stall-detector background task (already polls every 10 s).

### Network share transfer after extraction
After each clip extracts, optionally move to a configured destination
(NFS/SMB, assumed mounted). On success: local copy removed. On failure:
local file kept, warning in meta. Config unset = skip transfer.


## P4 — Tests still wanted

- Assert no orphaned ffmpeg process after clean shutdown
- Assert ALSA device released after clean shutdown
- Stall detector: mock a static session file, assert warning appears in status
- Meta read/write/atomic-update unit tests (no hardware)
- FINALIZING recovery: meta with pending extractions + stub MP4 → correct outputs
- Unexpected ffmpeg exit during CAPTURING → assert server transitions to INDEX,
  meta records cause
