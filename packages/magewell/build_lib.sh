#!/usr/bin/env bash
# Build libMWCapture.so natively from the vendored Magewell SDK static archive.
#
# Produces src/magewell/_lib/libMWCapture.so with proper DT_NEEDED entries, so it
# loads with a plain ctypes.CDLL (no RTLD_GLOBAL preload). No -dev packages are
# required: we point the linker at the system *runtime* libs via LIBRARY_PATH.
# This mirrors Magewell's gen_shared.sh linkage (CLIB="-lpthread -ldl -ludev
# -lasound -lv4l2"). See DECISIONS.md §"The native .so".
set -euo pipefail

SDK_VER="${SDK_VER:-3.3.1.1515}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ven="$here/vendor/mwcapture-sdk-$SDK_VER"
out="$here/src/magewell/_lib"

# map host architecture to the SDK's directory name
case "$(uname -m)" in
  x86_64)  arch=x64  ; libdir="/usr/lib/x86_64-linux-gnu" ;;
  aarch64) arch=arm64 ; libdir="/usr/lib/aarch64-linux-gnu" ;;
  *)       echo "error: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

a="$ven/Lib/$arch/libMWCapture.a"

[ -f "$a" ] || { echo "error: missing vendored archive $a" >&2; exit 1; }
command -v g++ >/dev/null || { echo "error: g++ not found" >&2; exit 1; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# link-time symlinks to the runtime libs (so -lasound/-ludev/-lv4l2 resolve
# without installing the corresponding -dev packages)
mkdir -p "$tmp/linkstubs"
for pair in libasound.so:libasound.so.2 libudev.so:libudev.so.1 libv4l2.so:libv4l2.so.0; do
  ln -sf "$libdir/${pair#*:}" "$tmp/linkstubs/${pair%%:*}"
done

( cd "$tmp" && ar x "$a" )
mkdir -p "$out"
LIBRARY_PATH="$tmp/linkstubs" g++ -shared -fPIC -O2 \
  -o "$out/libMWCapture.so" "$tmp"/*.o \
  -lpthread -ldl -ludev -lasound -lv4l2

echo "built: $out/libMWCapture.so"
readelf -d "$out/libMWCapture.so" | grep NEEDED || true
