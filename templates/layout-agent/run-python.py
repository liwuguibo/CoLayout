"""Validate the client runtime and replace this process with its pinned interpreter."""
import json
import os
from pathlib import Path
import sys


folder = Path(__file__).resolve().parent
path = folder / 'installation.json'
if not path.is_file():
    raise RuntimeError('Ask the developer to provide the local installation.json.')
settings = json.loads(path.read_text(encoding='utf8'))
if (settings.get('schema_version') != 'colayout-customer-runtime-1'
        or settings.get('connection_mode') != 'local-descriptor'):
    raise RuntimeError('Ask the developer to update the test-local runtime configuration.')
python = Path(settings.get('python_executable', ''))
if not python.is_absolute():
    raise RuntimeError('python_executable must be absolute.')
os.chdir(folder)
os.execv(str(python), [str(python), '-E', '-S', '-X', 'utf8', *sys.argv[1:]])
