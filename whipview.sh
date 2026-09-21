#!/usr/bin/env bash
# Whip-View launcher (Linux / macOS / Git Bash).
# Usage: ./whipview.sh [project-root] [--port N] [--no-browser]
# Project root: 1st argument > first line of whipview.local > current folder.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
proj=""
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then proj="$1"; shift; fi
if [ -z "$proj" ] && [ -f "$here/whipview.local" ]; then proj="$(head -n1 "$here/whipview.local" | tr -d '\r')"; fi
[ -z "$proj" ] && proj="$(pwd)"
# Skip stubs (e.g. the Windows Store "python3" alias) by actually running each candidate.
for c in python3 python py; do
  if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
    exec "$c" "$here/whipview.py" "$proj" "$@"
  fi
done
echo "Python 3.9+ not found." >&2; exit 1
