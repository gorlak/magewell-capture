#!/usr/bin/env bash
# run.sh — launch monitor mode.
#
# USAGE:
#   ./run.sh [--output-dir DIR] [--port PORT]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$HERE/.venv/bin/python" ]; then
    echo "virtualenv not found — run:  uv sync" >&2
    exit 1
fi

exec "$HERE/.venv/bin/python" "$HERE/scripts/monitor.py" "$@"
