# Security

## Agentic authorship

The majority of this codebase was written with substantial assistance from
agentic AI coding tools (Claude Code / Anthropic). The architecture,
requirements, and key design decisions are human-directed and documented in
[DECISIONS.md](DECISIONS.md). All code was reviewed and accepted by the human
author before being committed.

If you are evaluating this project for use in your own environment, treat it as
you would any open-source project from a small team: read the code, review the
design record, and run the tests before deploying. The test suite
(`uv run pytest`) covers the ffmpeg command builders and core logic.
Device-dependent paths require hardware to exercise.

## Privilege model

### At install time (`make install`)

`make install` requires `sudo` for two scoped writes:

- `/etc/udev/rules.d/70-magewell.rules` — grants your user group read-write
  access to the Magewell's USB and HID device nodes. Without it the SDK returns
  `EACCES` when opening the device.
- `/etc/systemd/system/magewell-capture.service` — the systemd unit file that
  starts the monitor on boot.

No files are written outside these two paths and the repository directory. No
packages are downloaded. No network access occurs during install.

### At runtime

The service runs as your regular user account — not root. The only non-standard
privilege is `CAP_NET_BIND_SERVICE`, granted via the systemd unit's
`AmbientCapabilities` directive, which allows binding to ports below 1024
(specifically :80 and :81). This capability applies only to the service process
and is not inherited by subprocesses.

`make run` (interactive / dev mode) runs with no elevated privileges and uses
ports :8090 / :8091, which require no capability grant.

## Network surface

| Port  | Protocol  | Bound to  | Purpose                              |
|-------|-----------|-----------|--------------------------------------|
| :80   | HTTP      | 0.0.0.0   | Web UI and JSON API (service mode)   |
| :81   | WebSocket | 0.0.0.0   | fMP4 live stream (service mode)      |
| :8090 | HTTP      | 0.0.0.0   | Web UI and JSON API (dev mode)       |
| :8091 | WebSocket | 0.0.0.0   | fMP4 live stream (dev mode)          |

The server binds to all interfaces. There is no TLS and no authentication. This
is intentional: the threat model is a physically-controlled machine on a trusted
LAN. If you expose this machine to an untrusted network, put a reverse proxy
with TLS and authentication in front of it.

The API endpoints (`/api/mark-in`, `/api/mark-out`) can start and stop
recording segments. Anyone who can reach the port can trigger a recording.

## Design decisions that may look like red flags

### Why not a Docker image?

Docker would require `--device` flags for the V4L2 video node, ALSA audio
passthrough, and the Magewell USB/HID nodes — all of which vary by host. The
udev rule would still need to be installed on the host regardless. NVENC
hardware video encoding inside Docker requires the NVIDIA Container Toolkit
with a driver version pinned to the image. For a single-machine appliance the
container boundary adds complexity without adding meaningful isolation. A
systemd service running as a normal user achieves the same process lifecycle
management with substantially less machinery.

### Why does the service run from the user's home directory?

The service writes recordings to the user's filesystem (configurable via
`transfer_dest` in `config.toml`, defaulting to `~/Downloads`). A dedicated
system user under `/opt` would need explicit grants for all of those paths, plus
group membership matching the udev device node grants. Since this is a
single-user appliance, running as the owner is simpler and equivalent in
practice.

### Why is the Magewell SDK vendored?

Magewell distributes their MWCapture SDK as a source archive. It is compiled
locally at setup time (`packages/magewell/build_lib.sh`) and never downloaded
at runtime. The SDK license is included at
`packages/magewell/vendor/mwcapture-sdk-<ver>/LICENSE.txt` and covers the
vendored SDK files only; it does not extend to this project.

### Why does the web UI have no login?

The recorder is designed for a trusted private network — a dedicated capture
machine on a production LAN with physical access controls. Adding
authentication to a local appliance with no multi-user requirement would be
complexity without a clear benefit. If your deployment requires access control,
place a reverse proxy (nginx, Caddy, etc.) with HTTP basic auth or mutual TLS
in front.

## Reporting issues

Please open an issue on GitHub. For sensitive findings, contact the author
directly via the email address in the repository's commit history.
