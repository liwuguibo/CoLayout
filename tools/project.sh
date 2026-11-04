#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python="$repo/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    echo 'Run setup.sh first.' >&2
    exit 1
fi
cd "$repo"
exec "$python" -X utf8 "$repo/tools/project.py" "$@"
