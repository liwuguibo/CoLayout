#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_version=3.13
bridge_arguments=()
while (($#)); do
    case "$1" in
        --python)
            python_version=${2:?'--python requires a version'}
            shift 2
            ;;
        --bridge-archive)
            bridge_arguments+=(--archive "${2:?'--bridge-archive requires a path'}")
            shift 2
            ;;
        -h|--help)
            echo 'Usage: ./setup.sh [--python 3.13] [--bridge-archive <archive>]'
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

command -v git >/dev/null || { echo 'Git is required. See docs/zh/setup.md.' >&2; exit 1; }
command -v uv >/dev/null || { echo 'uv is required: https://docs.astral.sh/uv/getting-started/installation/' >&2; exit 1; }
python_exe="$repo/.venv/bin/python"
if [[ ! -x "$python_exe" ]]; then
    uv venv --python "$python_version" "$repo/.venv"
fi
"$python_exe" -c "import sys; assert sys.version_info[:2] == (3,13), 'Use Python 3.13'"
"$python_exe" -X utf8 "$repo/tools/bootstrap_bridge.py" "${bridge_arguments[@]}"
uv pip install --python "$python_exe" -r "$repo/requirements-locked.txt"
uv pip install --python "$python_exe" --no-deps "$repo/.colayout/dependencies/virtuoso-bridge"
uv pip install --python "$python_exe" --no-deps -e "$repo"
cd "$repo"
"$python_exe" -X utf8 -m colayout_lite.bootstrap init
"$python_exe" -X utf8 tools/check_environment.py
echo 'Installed. Continue with AGENTS.md or docs/zh/setup.md. No server or layout was changed.'
