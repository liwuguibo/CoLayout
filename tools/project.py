"""Linux launcher and maintenance entry; Windows keeps tools/project.ps1."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
POINTER = ROOT / '.colayout/installation.json'


def existing_processes(pointer, proc_root=None):
    matches = []
    proc = Path(proc_root) if proc_root is not None else Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('The Linux launcher requires /proc for process identity checks.')
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / 'cmdline').read_bytes().split(b'\0')
            values = [value.decode(errors='surrogateescape') for value in arguments if value]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if 'colayout_lite.workspace_app' in values and str(pointer) in values:
            matches.append(int(entry.name))
    return matches


def start():
    settings = json.loads(POINTER.read_text(encoding='utf8'))
    existing = existing_processes(POINTER)
    if existing:
        print(f'This installation already has an APP process (PID {existing[0]}); no second writer started.')
        return 0
    log_path = ROOT / 'artifacts/lite/launcher.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('ab') as log:
        process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'colayout_lite.workspace_app',
             str(settings['document']), '--agent-api', '--installation', str(POINTER)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f'CoLayout started (PID {process.pid}). Use status to check readiness.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='status',
                        choices=('start', 'status', 'publish', 'test', 'doctor'))
    args = parser.parse_args()
    if args.action == 'start':
        if not POINTER.is_file():
            raise RuntimeError('Run setup.sh first.')
        return start()
    if args.action == 'test':
        return subprocess.call([sys.executable, '-X', 'utf8', '-m', 'pytest', 'tests_lite', '-q'], cwd=ROOT)
    if args.action in ('status', 'doctor'):
        return subprocess.call([sys.executable, '-X', 'utf8', '-m', 'colayout_lite.bootstrap', args.action], cwd=ROOT)
    if not POINTER.is_file():
        raise RuntimeError('Run setup.sh first.')
    return subprocess.call([sys.executable, '-X', 'utf8', '-m', 'tools.publish_test_connection',
                            '--installation', str(POINTER)], cwd=ROOT)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
