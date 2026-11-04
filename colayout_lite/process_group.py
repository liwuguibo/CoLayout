"""POSIX process-group lifetime matching the host-owned Windows job boundary."""
import os
import signal
import subprocess


def terminate(process, timeout=2):
    if os.name == 'nt':
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
