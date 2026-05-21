# Attributions

This project's own code is MIT-licensed (see `LICENSE`). It also bundles
third-party material and was informed by other projects, credited here.

## Vendored third-party code

### Magewell MWCapture SDK (Linux) 3.3.1.1515
- **Location:** `packages/magewell/vendor/mwcapture-sdk-3.3.1.1515/`
  — `Lib/x64/libMWCapture.a`, `Include/**/*.h`, `gen_shared.sh`.
- **Copyright** © Nanjing Magewell Electronics Co., Ltd. All rights reserved.
- **License:** the permissive (BSD/MIT-style) grant in the SDK header notice —
  use, publish, distribute, and sublicense permitted provided the copyright
  notice is retained and the disclaimer is included. Verbatim notice kept at
  `packages/magewell/vendor/mwcapture-sdk-3.3.1.1515/LICENSE.txt`.
- **Provenance:** downloaded from **magewell.com** (official SDK download page).
  SHA256 + retrieval date are recorded in that directory's `SOURCE.txt`. An
  earlier version (3.3.1.1313, from the ReproNim/reprostim mirror) had a USB
  interface/endpoint mismatch with the Gen2 hardware — 1515 resolved it.
- The built `libMWCapture.so` is generated locally from the vendored `.a` by
  `packages/magewell/build_lib.sh` and is gitignored.

## Projects consulted for code authoring

### ReproNim/reprostim — https://github.com/ReproNim/reprostim  (MIT)
We studied reprostim's `src/reprostim-capture` to **inform** (not copy) our work:
- the MWCapture SDK → ffmpeg pipeline pattern (SDK reads the signal; ffmpeg does
  the capture/encode);
- the signal-status read sequence in `capturelib/src/CaptureLib.cpp`
  (`MWCaptureInitInstance` → `MWRefreshDevice` → `MWGetDevicePath` →
  `MWOpenChannelByPath` → `MWGetVideoSignalStatus`);
- the udev-rule approach for non-root device access
  (`etc/udev/189-reprostim.rules`), which `packages/magewell/udev/70-magewell.rules`
  mirrors and extends to also cover the `hidraw` node.

reprostim is also the source mirror for the vendored Magewell SDK above.

Our `magewell` ctypes binding, `setup.sh` / `teardown.sh`, and the test suite are
original implementations.
