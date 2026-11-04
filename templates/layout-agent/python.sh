#!/usr/bin/env bash
set -euo pipefail
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$kit"
exec python3 -E -S -X utf8 "$kit/run-python.py" "$@"
