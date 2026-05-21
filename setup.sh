#!/usr/bin/env bash
#
# setup.sh — one-time privileged setup for the magewell-capture box.
#
# WHAT IT DOES (and nothing more):
#   Installs a single udev rule so the Magewell capture device is read-write for
#   members of the "plugdev" group. The MWCapture SDK needs two device nodes that
#   are root-only by default:
#     * the raw USB node  (/dev/bus/usb/BBB/DDD)  — to open the capture channel
#     * the HID interface (/dev/hidrawN)          — for control/status transfers,
#       e.g. reading the input signal (MWGetVideoSignalStatus)
#   Without USB access the open fails (EACCES); with USB but not hidraw the
#   channel opens but every SDK call returns MW_FAILED. ffmpeg/V4L2 capture is
#   unaffected (it uses /dev/videoN, group "video").
#
# WHY IT NEEDS ROOT:
#   Writing under /etc/udev/rules.d and reloading udev require root. That is the
#   ONLY reason for sudo. There are no downloads and no network access, and the
#   only file created is the udev rule shown below — which is printed before it
#   is installed so you can read exactly what is being applied.
#
# USAGE:
#   sudo ./setup.sh
#
set -euo pipefail

# Resolve paths relative to this script, so it works from any directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/packages/magewell/udev/70-magewell.rules"   # the rule shipped in this repo
DEST="/etc/udev/rules.d/70-magewell.rules"

# 1) Require root (we are about to write under /etc and talk to udev).
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root:  sudo ./setup.sh" >&2
    exit 1
fi

# 2) The rule must exist; show it so you can see precisely what gets installed.
if [ ! -f "$SRC" ]; then
    echo "error: rule file not found at $SRC" >&2
    exit 1
fi
echo "About to install this udev rule:"
echo "------------------------------------------------------------------------"
cat "$SRC"
echo "------------------------------------------------------------------------"

# 3) Install it as an ordinary, world-readable config file (root-owned, 0644).
install -m 0644 -o root -g root "$SRC" "$DEST"
echo "installed: $DEST"

# 4) Reload udev and re-apply rules to already-connected devices (no replug).
udevadm control --reload
udevadm trigger --subsystem-match=usb --attr-match=idVendor=2935
udevadm trigger --subsystem-match=hidraw
echo "udev rules reloaded and re-applied."

# 5) Show the resulting permissions for any attached Magewell device.
echo
echo "Magewell USB node permissions now:"
found=0
for d in /sys/bus/usb/devices/*; do
    if [ -r "$d/idVendor" ] && [ "$(cat "$d/idVendor")" = "2935" ]; then
        node="$(printf '/dev/bus/usb/%03d/%03d' "$(cat "$d/busnum")" "$(cat "$d/devnum")")"
        ls -l "$node"
        found=1
    fi
done
[ "$found" -eq 1 ] || echo "  (no Magewell device currently attached — rule still installed)"

echo "HID node(s) (the Magewell's should now be group plugdev, mode 0660):"
ls -l /dev/hidraw* 2>/dev/null || echo "  (none present)"

echo
echo "Done. Members of the 'plugdev' group can now reach the Magewell device."
echo "Verify your user is in plugdev:  groups | tr ' ' '\\n' | grep -qx plugdev && echo yes"
