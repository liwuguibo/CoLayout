"""Linux desktop and process lifecycle regressions using synthetic workers only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from colayout_lite.process_group import terminate
from tools.install_workspace_shortcut import desktop_entry
from tools.project import existing_processes


pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux portability')


def wait_for(path, process):
    deadline = time.monotonic() + 10
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists(), 'Synthetic child did not start'


def process_gone(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(')', 1)[1].strip().startswith('Z')


def test_process_group_termination_includes_descendants(tmp_path):
    child_pid = tmp_path / 'child.pid'
    code = (
        'import pathlib,subprocess,sys,time;'
        'p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"]);'
        'pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)'
    )
    process = subprocess.Popen([sys.executable, '-c', code, str(child_pid)],
                               start_new_session=True)
    try:
        wait_for(child_pid, process)
        child = int(child_pid.read_text())
        assert os.getsid(process.pid) == process.pid
        terminate(process)
        assert process.poll() is not None
        deadline = time.monotonic() + 5
        while not process_gone(child) and time.monotonic() < deadline:
            time.sleep(.02)
        assert process_gone(child)
    finally:
        if process.poll() is None:
            terminate(process)


def test_process_group_cleanup_survives_launcher_exit(tmp_path):
    child_pid = tmp_path / 'orphan.pid'
    code = (
        'import pathlib,subprocess,sys;'
        'p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"]);'
        'pathlib.Path(sys.argv[1]).write_text(str(p.pid))'
    )
    process = subprocess.Popen([sys.executable, '-c', code, str(child_pid)],
                               start_new_session=True)
    wait_for(child_pid, process)
    process.wait(timeout=10)
    child = int(child_pid.read_text())
    terminate(process)
    deadline = time.monotonic() + 5
    while not process_gone(child) and time.monotonic() < deadline:
        time.sleep(.02)
    assert process_gone(child)


def test_read_worker_is_a_process_group_leader_and_stops(tmp_path):
    from colayout_lite.agent_tools.read_pool import ReadWorker
    from threading import Event

    worker = ReadWorker(tmp_path, Event())
    try:
        worker.start()
        process = worker.process
        assert os.getsid(process.pid) == process.pid
    finally:
        worker.stop()
    assert process.poll() is not None


def test_process_probe_requires_module_and_exact_installation(tmp_path):
    proc = tmp_path / 'proc'
    selected = tmp_path / 'selected/installation.json'
    for pid, arguments in (
        (101, ['python', '-m', 'colayout_lite.workspace_app', 'layout.json',
               '--installation', str(selected)]),
        (102, ['python', '-m', 'colayout_lite.workspace_app', 'layout.json',
               '--installation', str(tmp_path / 'other/installation.json')]),
        (103, ['python', '-m', 'another.module', '--installation', str(selected)]),
    ):
        folder = proc / str(pid)
        folder.mkdir(parents=True)
        (folder / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in arguments) + b'\0')
    assert existing_processes(selected, proc) == [101]


def test_desktop_entry_uses_checkout_launcher_and_icon(tmp_path):
    root = tmp_path / 'source with spaces'
    content = desktop_entry(root)
    assert 'Exec="' + str(root / 'Start-CoLayout.sh') + '"' in content
    assert 'Icon=' + str(root / 'colayout_lite/assets/branding/colayout-e2.png') in content
    assert 'Terminal=false' in content
    assert 'StartupWMClass=colayout-lite' in content


def test_linux_desktop_identity_and_chinese_font():
    from PySide6.QtWidgets import QApplication
    from colayout_lite.app_identity import configure_application, DESKTOP_FILE_NAME
    from colayout_lite.fonts import ui_family, ui_font

    app = QApplication.instance() or QApplication([])
    configure_application(app)
    assert app.desktopFileName() == DESKTOP_FILE_NAME
    assert app.property('colayoutLinuxIdentity')
    assert not app.property('colayoutWindowsIdentity')
    assert ui_family()
    assert ui_font(pixel_size=12).pixelSize() == 12


def test_posix_client_runner_validates_and_executes(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'templates/layout-agent/run-python.py'
    target = tmp_path / 'run-python.py'
    target.write_bytes(source.read_bytes())
    script = tmp_path / 'client.py'
    script.write_text('import json,sys;print(json.dumps([sys.flags.ignore_environment,sys.flags.no_site]))')
    (tmp_path / 'installation.json').write_text(json.dumps({
        'schema_version': 'colayout-customer-runtime-1',
        'connection_mode': 'local-descriptor',
        'python_executable': sys.executable,
    }))
    result = subprocess.run([sys.executable, '-E', '-S', str(target), str(script)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, 1]


def test_bridge_pid_probe_posix():
    from virtuoso_bridge.transport.tunnel import _pid_is_alive

    assert _pid_is_alive(os.getpid())
    assert not _pid_is_alive(-1)
