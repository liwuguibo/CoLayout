# ruff: noqa: F811
from time import monotonic

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QGraphicsScene, QMainWindow

from colayout_lite.editor import Editor
from colayout_lite.model import Shape
from colayout_lite.parasitic_view import ParasiticView
from colayout_lite.presentation.canvas import Canvas
from tests_lite.test_parasitic_rc import profile, rc
from tests_lite.test_parasitic_workspace import blackbox_workspace, guard_blackbox


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class Selection:
    def __init__(self, ids):
        self.value = list(ids)

    def ids(self):
        return list(self.value)


class Host(QMainWindow):
    view_state_changed = Signal()


def wait_until(app, predicate, seconds=3):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(15)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.start()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()
    timer.stop()
    app.processEvents()
    return predicate()


def host_window(profile, monkeypatch):
    guard_blackbox(monkeypatch)
    host = Host()
    host.editor = Editor(blackbox_workspace(profile), profile)
    host.human = Selection(['wire'])
    host.canvas = Canvas(QGraphicsScene(host))
    host.setCentralWidget(host.canvas)
    host.parasitics = ParasiticView(host)
    host.parasitics.show()
    host.resize(900, 650)
    host.show()
    return host


def test_readonly_panel_computes_and_close_cancels(app, profile, monkeypatch):
    host = host_window(profile, monkeypatch)
    view = host.parasitics
    before = host.editor.w.revision
    try:
        assert wait_until(app, lambda: view.estimate is not None)
        assert view.estimate.resistance_ohm == 1
        assert view.estimate.ground_capacitance_fF == 31
        assert view.estimate.coupling_capacitance_fF > 0
        assert host.editor.w.revision == before
        view.close()
        pending = view.pending
        assert view.closed and view.estimate is None
        assert host.editor.w.revision == before
    finally:
        host.close()


def test_continuous_requests_still_publish_during_motion(app, profile, monkeypatch):
    host = host_window(profile, monkeypatch)
    view = host.parasitics
    seen = set()
    running = {'value': True}
    timer = QTimer(host)
    timer.setInterval(25)
    timer.timeout.connect(lambda: view.submit(['wire']) if running['value'] else None)
    watchdog = QTimer(host)
    watchdog.setInterval(20)
    watchdog.timeout.connect(lambda: seen.add(view.estimate_key) if view.estimate_key is not None else None)
    try:
        timer.start()
        watchdog.start()
        assert wait_until(app, lambda: len(seen) >= 3, seconds=5.5)
        assert host.editor.w.revision == 0
    finally:
        running['value'] = False
        timer.stop()
        watchdog.stop()
        host.close()


def test_rc_disable_and_failure_never_block_editing(app, profile, monkeypatch):
    host = host_window(profile, monkeypatch)
    view = host.parasitics
    try:
        view.set_enabled(False)
        view.refresh()
        assert not view.enabled and not view.panel.enabled.isChecked()
        before = host.editor.w.revision
        host.editor.create(Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 10000), (20000, 10000)), width=2000))
        assert host.editor.w.revision == before + 1
        monkeypatch.setattr('colayout_lite.parasitic_view.analyze_workspace', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
        view.set_enabled(True)
        assert wait_until(app, lambda: 'RC 计算未完成' in view.panel.status.text())
        host.editor.create(Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 14000), (20000, 14000)), width=2000))
        assert host.editor.w.revision == before + 2
    finally:
        host.close()


def test_commit_event_preserves_before_after_for_undo_and_redo(app, profile, monkeypatch):
    host = host_window(profile, monkeypatch)
    events = []
    unsubscribe = host.editor.subscribe_commits(events.append)
    try:
        created = host.editor.create(Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 10000), (20000, 10000)), width=2000))
        assert events[-1].before.revision == 0 and events[-1].after.revision == 1
        assert created in events[-1].changed
        host.editor.undo()
        assert events[-1].before.revision == 1 and events[-1].after.revision == 2
        assert created in events[-1].changed and created not in events[-1].after.operations
        host.editor.redo()
        assert events[-1].before.revision == 2 and events[-1].after.revision == 3
        assert created in events[-1].changed and created in {op.uid for op in events[-1].after.operations}
    finally:
        unsubscribe()
        host.close()


def test_process_editor_rc_json_roundtrip(app, profile):
    from colayout_lite.process_editor import ConfigurationEditor
    from colayout_lite.process_package import ProcessPackage
    widget = ConfigurationEditor()
    try:
        widget.set_package(ProcessPackage(technology=profile))
        assert widget.value().technology.rc_model_digest == profile.rc_model_digest
        widget.parasitics.setPlainText('')
        assert widget.value().technology.parasitics is None
        widget.parasitics.setPlainText('{invalid')
        with pytest.raises(Exception):
            widget.value()
    finally:
        widget.close()
