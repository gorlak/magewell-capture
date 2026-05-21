#!/usr/bin/env bash
#
# teardown.sh — reverse of setup.sh.
#
# Removes the udev rule that setup.sh installed, so the Magewell USB node
# reverts to its default (root-only) permissions. Use this to undo the
# privileged setup — e.g. to verify it's the rule that grants access, or before
# decommissioning the box.
#
# As with setup.sh, root is needed only to remove the file under
# /etc/udev/rules.d and reload udev. No downloads, no network.
#
# USAGE:
#   sudo ./teardown.sh
#
set -euo pipefail

DEST="/etc/udev/rules.d/70-magewell.rules"

# 1) Require root (we are about to remove a file under /etc and talk to udev).
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root:  sudo ./teardown.sh" >&2
    exit 1
fi

# 2) Remove the rule (idempotent — fine if it's already gone).
if [ -f "$DEST" ]; then
    rm -f "$DEST"
    echo "removed: $DEST"
else
    echo "nothing to remove: $DEST is not present"
fi

# 3) Reload udev and re-apply rules so the node reverts to default permissions
#    (no replug needed).
udevadm control --reload
udevadm trigger --subsystem-match=usb --attr-match=idVendor=2935
udevadm trigger --subsystem-match=hidraw
echo "udev rules reloaded and re-applied."

# 4) Show the resulting permissions for any attached Magewell device.
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
[ "$found" -eq 1 ] || echo "  (no Magewell device currently attached)"

echo
echo "Done. The access rule has been removed; the node is back to root-only."
echo "(A replug or reboot also fully resets the device to defaults.)"
