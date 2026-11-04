"""The pour UI keeps 45-degree corners optional and passes the choice to the shared service."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsScene, QLabel, QMainWindow

from colayout_lite.editor import Editor
from colayout_lite.model import Snapshot, Workspace
from colayout_lite.pour_panel import PourPanel
from colayout_lite.presentation.canvas import Canvas
from colayout_lite.technology import TechnologyProfile


class Selection:
    def __init__(self):
        self.value = []

    def ids(self):
        return list(self.value)

    def set_ids(self, value):
        self.value = list(value)

    def escape(self):
        pass

    def cancel(self):
        pass


class Window(QMainWindow):
    view_state_changed = Signal()


@pytest.fixture
def editor():
    profile = TechnologyProfile(
        id='synthetic',
        label='Synthetic technology',
        native_fingerprint='fp',
        dbu_per_micron=2000,
        grid=2,
        writable_lpps=(('wire_a', 'drawing'),),
        layers=[{'layer': 'wire_a', 'purpose': 'drawing', 'material': 'metal'}],
        vias={},
    )
    snapshot = Snapshot(
        library='L',
        cell='C',
        library_path='/test/L',
        session='s',
        counter='1',
        dirty=False,
        dbu_per_micron=2000,
        grid=2,
        objects={},
        technology_fingerprint='fp',
    )
    return Editor(Workspace(baseline=snapshot, profile_id=profile.id), profile)


@pytest.fixture
def pour_ui(editor):
    app = QApplication.instance() or QApplication([])
    window = Window()
    selection = Selection()
    window.editor = editor
    window.busy = False
    window.human = selection
    window.selection_ui = selection
    window.drc = SimpleNamespace(profile=None, pick=SimpleNamespace(setChecked=lambda _: None))
    window.status = QLabel()
    window.canvas = Canvas(QGraphicsScene(window))
    panel = PourPanel(window)
    window.pour_panel = panel
    window.setCentralWidget(panel)
    window.resize(500, 400)
    window.show()
    app.processEvents()
    try:
        yield app, window, panel
    finally:
        window.close()
        app.processEvents()


def capture_execute(monkeypatch):
    calls = []

    def fake_execute(editor, action, params, *, rules=None):
        calls.append((editor, action, params, rules))
        return {
            'created_uids': ['pour'],
            'polygon_count': 1,
            'area_um2': 100,
            'discarded_island_count': 0,
        }

    monkeypatch.setattr('colayout_lite.layout_helpers.service.execute', fake_execute)
    return calls


def start_pour(app, panel):
    QTest.keyClicks(panel.net, 'VSS')
    QTest.mouseClick(panel.draw, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert panel.context is not None
    assert panel.window.canvas.region_select


@pytest.mark.parametrize('checked', [False, True])
def test_allow_45_defaults_off_and_reaches_create_pour(pour_ui, monkeypatch, checked):
    app, window, panel = pour_ui
    calls = capture_execute(monkeypatch)
    assert panel.allow_45.text() == '允许 45° 拐弯'
    assert panel.allow_45.isChecked() is False

    if checked:
        QTest.mouseClick(panel.allow_45, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
        app.processEvents()
    assert panel.allow_45.isChecked() is checked
    start_pour(app, panel)
    panel.picked((0, 0, 20000, 20000))
    app.processEvents()

    assert len(calls) == 1
    assert calls[0][0] is window.editor
    assert calls[0][1] == 'create_pour'
    assert calls[0][2]['allow_45'] is checked
    assert panel.context is None
    assert window.canvas.region_select is False


def test_cancel_and_busy_state_preserve_allow_45_choice(pour_ui):
    app, window, panel = pour_ui
    QTest.mouseClick(panel.allow_45, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
    start_pour(app, panel)

    window.canvas.region_cancelled.emit()
    app.processEvents()
    assert panel.context is None
    assert panel.draw.isChecked() is False
    assert window.canvas.region_select is False
    assert panel.allow_45.isChecked() is True

    window.busy = True
    panel.refresh()
    assert panel.allow_45.isEnabled() is False
    assert panel.draw.isEnabled() is False
    window.busy = False
    panel.refresh()
    assert panel.allow_45.isEnabled() is True
    assert panel.draw.isEnabled() is True
    assert panel.allow_45.isChecked() is True
