# magewell-capture

Headless HDMI capture + archival appliance built around a Magewell USB Capture
HDMI Gen2. A `magewell` Python package wraps the Magewell MWCapture SDK (via
ctypes) to read the live input signal; capture/encode is done by ffmpeg (NVENC).

See **[DECISIONS.md](DECISIONS.md)** for the full design record, findings, and
rationale, and **[ATTRIBUTIONS.md](ATTRIBUTIONS.md)** for third-party credits.

## Layout

```
packages/magewell/   ctypes wrapper + vendored SDK + native-lib build script
scripts/             loose, editable capture scripts (run via the venv)
setup.sh             one-time privileged setup (udev rule) — undo: teardown.sh
DECISIONS.md         design record
ATTRIBUTIONS.md      third-party / consulted-project credits
```

The Magewell SDK license notice lives with the vendored SDK it governs
(`packages/magewell/vendor/mwcapture-sdk-<ver>/LICENSE.txt`).

## Bootstrap (Debian 13 / this box)

```bash
# tooling (one-time)
sudo apt install -y pipx ffmpeg v4l-utils
pipx install uv

# one-time privileged setup — installs a udev rule so the capture user (group
# plugdev) can reach the Magewell's device nodes (raw USB + HID), which the SDK
# opens read-write to read signal status (otherwise: MWAccessError / EACCES).
# The script prints the exact rule before installing it; read it first if you
# like. Root is needed only to write under /etc/udev and reload udev — no
# downloads, no network.  (Undo any time with:  sudo ./teardown.sh)
sudo ./setup.sh

# build the native Magewell lib, then create the venv with all packages
packages/magewell/build_lib.sh
uv sync

# run the tests (layout tests always run; device tests need the Magewell attached)
uv run pytest

# run a capture script against the venv
.venv/bin/python scripts/capture.py
# (optional) expose on PATH:  ln -s "$PWD/scripts/capture.py" ~/.local/bin/capture
```

The native `libMWCapture.so` is built per-box from the vendored SDK archive and
is gitignored; everything needed to rebuild it is committed.
