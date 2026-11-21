# ruff: noqa: F811
import pytest
from PySide6.QtCore import QEventLoop, QTimer, Signal
from PySide6.QtWidgets import QApplication, QGraphicsScene, QMainWindow

import colayout_lite.parasitic_workspace as parasitic_workspace
from colayout_lite.editor import Editor
from colayout_lite.model import Shape
from colayout_lite.parasitic_view import ParasiticView
from colayout_lite.presentation.canvas import Canvas
from tests_lite.test_parasitic_rc import profile, rc  # noqa: F401
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


def host_window(profile, monkeypatch):  # noqa: F811
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


def test_ondemand_panel_never_computes_until_requested_and_clears_cleanly(app, profile, monkeypatch):  # noqa: F811
    calls = []
    real_eval = parasitic_workspace.evaluate_parasitics_outcome

    def spy_eval(*args, **kwargs):
        calls.append(kwargs.get('uids'))
        return real_eval(*args, **kwargs)

    monkeypatch.setattr('colayout_lite.parasitic_view.evaluate_parasitics_outcome', spy_eval)
    host = host_window(profile, monkeypatch)
    view = host.parasitics
    before = host.editor.w.revision
    try:
        # Launching, refreshing, changing radius, and committing geometry perform zero unsolicited RC work
        host.view_state_changed.emit()
        view.panel.radius.setValue(12.0)
        created = host.editor.create(
            Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 10000), (20000, 10000)), width=2000)
        )
        app.processEvents()
        assert calls == []
        assert view.estimate is None

        # Explicit user click evaluates selected wire without mutating revision
        host.human.value = ['wire']
        view.panel.evaluate_button.click()
        assert wait_until(app, lambda: view.estimate is not None)
        assert len(calls) == 1
        assert view.estimate.resistance_ohm == 1
        assert view.estimate.ground_capacitance_fF == 31
        assert view.estimate.coupling_capacitance_fF > 0
        assert host.editor.w.revision == before + 1

        # Subsequent edit marks estimate stale without background recompute, and evaluate updates with delta
        extra = host.editor.create(
            Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 14000), (20000, 14000)), width=2000)
        )
        assert len(calls) == 1
        assert '未自动重算' in view.panel.status.text()
        host.human.value = [extra]
        view.panel.evaluate_button.click()
        assert wait_until(app, lambda: len(calls) == 2 and view.panel.delta.text() != '—')
        assert created in host.editor.w.baseline.objects or created in {op.uid for op in host.editor.w.operations}

        view.panel.clear_button.click()
        assert view.estimate is None and view.panel.resistance.text() == '—'
        view.close()
        assert view.closed and view.estimate is None
    finally:
        host.close()


def test_ondemand_failure_never_blocks_editing(app, profile, monkeypatch):  # noqa: F811
    host = host_window(profile, monkeypatch)
    view = host.parasitics
    try:
        before = host.editor.w.revision
        monkeypatch.setattr(
            'colayout_lite.parasitic_view.evaluate_parasitics_outcome',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic failure')),
        )
        view.panel.evaluate_button.click()
        assert wait_until(app, lambda: 'RC 计算未完成' in view.panel.status.text())
        host.editor.create(
            Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 10000), (20000, 10000)), width=2000)
        )
        assert host.editor.w.revision == before + 1
    finally:
        host.close()


def test_commit_event_preserves_before_after_for_undo_and_redo(app, profile, monkeypatch):  # noqa: F811
    host = host_window(profile, monkeypatch)
    events = []
    unsubscribe = host.editor.subscribe_commits(events.append)
    try:
        created = host.editor.create(
            Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, 10000), (20000, 10000)), width=2000)
        )
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


def test_process_editor_rc_json_roundtrip(app, profile):  # noqa: F811
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
