"""Local asynchronous RC estimates and disposable canvas overlays."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from time import monotonic

from PySide6.QtCore import QObject, QRectF, Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QCheckBox, QDockWidget, QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from .editor import projected
from .geometry import transform_shape
from .inspection_view import polygon_path
from .parasitic_rc import GeometrySet, RcBudget, RcCancelled, compare_estimates, merge_geometry_sets, shape_geometry
from .parasitic_workspace import analyze_workspace
from .theme import alpha, palette


@dataclass
class AnalysisRequest:
    editor: object
    workspace: object
    profile: object
    scope: object
    peek: bool
    target_uids: object
    extra_target: GeometrySet
    exclude_uids: tuple
    baseline_workspace: object
    baseline_target_uids: object
    source_workspace: object
    candidate: bool
    budget: RcBudget
    generation: int
    requested: float
    cancelled: Event


@dataclass(frozen=True)
class AnalysisOutcome:
    estimate: object
    delta: object = None
    has_baseline: bool = False


class RcPanel(QWidget):
    def __init__(self, owner):
        super().__init__(owner.window)
        self.owner = owner
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox('RC 寄生影响（本地轻量估算）')
        self.enabled.setToolTip('仅使用本地冻结几何和已审核参数；不替代 Calibre 或签核提取。')
        self.enabled.toggled.connect(owner.set_enabled)
        layout.addWidget(self.enabled)
        form = QFormLayout()
        self.resistance = QLabel('—')
        self.ground = QLabel('—')
        self.coupling = QLabel('—')
        self.delta = QLabel('—')
        form.addRow('R', self.resistance)
        form.addRow('Cg · 参考地', self.ground)
        form.addRow('Cc · 邻近耦合', self.coupling)
        form.addRow('ΔR / ΔCg / ΔCc', self.delta)
        layout.addLayout(form)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(.1, 100)
        self.radius.setValue(10)
        self.radius.setSuffix(' µm')
        self.radius.setSingleStep(.5)
        self.radius.valueChanged.connect(owner.set_radius)
        form.addRow('近邻范围', self.radius)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QLabel('Cg 不包含显式邻近耦合；每个耦合对只计一次。\nRC 失败或超时不会阻止正常布线。')
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        layout.addStretch(1)

    @staticmethod
    def number(value, unit):
        return '—' if value is None else f'{value:.6g} {unit}'

    def set_resistance(self, value, status='complete'):
        self.resistance.setText(self.number(value, 'Ω') + ('' if status == 'complete' else f' · {status}'))

    def set_estimate(self, outcome):
        estimate = outcome.estimate
        self.set_resistance(estimate.resistance_ohm, estimate.resistance_status)
        self.ground.setText(self.number(estimate.ground_capacitance_fF, 'fF') + ' · ' + estimate.ground_status)
        self.coupling.setText(self.number(estimate.coupling_capacitance_fF, 'fF') + ' · ' + estimate.coupling_status)
        delta = outcome.delta
        if not outcome.has_baseline:
            self.delta.setText('—')
        else:
            self.delta.setText('覆盖不一致，不能直接比较总量' if delta is None else
                f'{delta["delta_r_ohm"]:+.4g} Ω / {delta["delta_cg_fF"]:+.4g} fF / {delta["delta_cc_fF"]:+.4g} fF'
                if delta['delta_r_ohm'] is not None else
                f'— / {delta["delta_cg_fF"]:+.4g} fF / {delta["delta_cc_fF"]:+.4g} fF')
        mode_label = '神经网络估算' if getattr(estimate, 'mode', 'analytic') != 'analytic' else '本地计算完成'
        timing = f' ({estimate.elapsed_ms:.2f} ms)' if getattr(estimate, 'elapsed_ms', None) is not None else ''
        self.status.setText('\n'.join(estimate.warnings) if estimate.warnings else f'模型范围内 · {mode_label}{timing}')


class ParasiticView(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.panel = RcPanel(self)
        self.dock = QDockWidget('RC 寄生影响', window)
        self.dock.setObjectName('parasiticDock')
        self.dock.setWidget(self.panel)
        window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-rc-view')
        self.future = None
        self.active = None
        self.pending = None
        self.generation = 0
        self.estimate = None
        self.estimate_key = None
        self.request_key = None
        self.enabled = bool(window.editor and window.editor.profile.parasitics)
        self.closed = False
        self.bound_editor = None
        self.commit_unsubscribe = None
        self.gesture_baseline = None
        self.candidate_source = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(20)
        self._poll_timer.timeout.connect(self._poll)
        self._heat_timer = QTimer(self)
        self._heat_timer.setInterval(175)
        self._heat_timer.timeout.connect(self._sample_heat)
        self._light_timer = QTimer(self)
        self._light_timer.setInterval(50)
        self._light_timer.timeout.connect(self._sample_light)
        window.canvas.parasitic_overlay = self.paint
        window.view_state_changed.connect(self.refresh)
        self.dock.visibilityChanged.connect(self._visibility)
        for timer in (self._poll_timer, self._heat_timer, self._light_timer):
            timer.start()
        self.panel.enabled.setChecked(self.enabled)
        self.dock.setVisible(self.enabled)
        self.refresh()

    def _visibility(self, visible):
        if not visible:
            self._cancel_all(clear=True)
        else:
            self.refresh()
        self.window.view_state_changed.emit()

    def set_enabled(self, enabled):
        with QSignalBlocker(self.panel.enabled):
            self.panel.enabled.setChecked(bool(enabled))
        self.enabled = bool(enabled)
        if not self.enabled:
            self._cancel_all(clear=True)
        else:
            self.request_key = None
            self.refresh()

    def set_radius(self, value):
        self.request_key = None
        self.refresh()

    def show(self):
        if getattr(getattr(self.window, 'studio', None), 'mode', None) == 'studio':
            from .tool_panels import show_tool

            show_tool(self.window, self.dock)
        else:
            self.dock.show()
            self.dock.raise_()
        self.refresh()

    def toggle(self, *_):
        if not self.dock.isHidden():
            studio = getattr(getattr(self.window, 'studio', None), 'mode', None) == 'studio'
            if studio:
                from .camera import capture, restore

                state = capture(self.window.canvas)
                self.dock.hide()
                self.window.layout().activate()
                restore(self.window.canvas, state)
                from .presentation.dock_tabs import refresh_tabs

                refresh_tabs(self.window)
            else:
                self.dock.hide()
        else:
            self.show()

    def _cancel_all(self, clear=False):
        self.generation += 1
        for request in (self.active, self.pending):
            if request:
                request.cancelled.set()
        self.pending = None
        self.request_key = None
        self.candidate_source = None
        if clear:
            self.estimate = None
            self.estimate_key = None
            self.window.canvas.viewport().update()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cancel_all(clear=True)
        if self.commit_unsubscribe:
            self.commit_unsubscribe()
            self.commit_unsubscribe = None
        self.bound_editor = None
        for timer in (self._poll_timer, self._heat_timer, self._light_timer):
            timer.stop()
        if self.future:
            self.future.cancel()
        self.future = None
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _bind_editor(self):
        editor = self.window.editor
        if editor is self.bound_editor:
            return
        if self.commit_unsubscribe:
            self.commit_unsubscribe()
        self._cancel_all(clear=True)
        self.bound_editor = editor
        self.commit_unsubscribe = editor.subscribe_commits(self.commit) if editor else None

    def _key(self, target):
        editor = self.window.editor
        return (id(editor), id(editor.w), editor.scope_id, self.window.canvas.blackbox_peek > 0,
                target, editor.profile.rc_model_digest, self.panel.radius.value())

    def refresh(self):
        if self.closed:
            return
        self._bind_editor()
        editor = self.window.editor
        if not editor:
            return
        configured = bool(editor.profile.parasitics)
        if not configured and self.panel.enabled.isChecked():
            self.panel.enabled.setChecked(False)
        self.enabled = configured and self.panel.enabled.isChecked()
        if not configured or not self.enabled or self.dock.isHidden():
            if self.estimate is not None:
                self._cancel_all(clear=True)
            self.panel.status.setText('请在工艺库配置并审核 RC 参数。' if not configured else 'RC 显示已暂停。')
            return
        human = getattr(self.window, 'human', None)
        ids = tuple(sorted(human.ids())) if human else ()
        target = ids or None
        key = self._key(target)
        if key == self.request_key:
            return
        self.request_key = key
        self.submit(target)

    def commit(self, event):
        editor = self.window.editor
        if self.closed or editor.profile is not event.profile or not self.enabled or self.dock.isHidden():
            if editor:
                self._cancel_all(clear=True)
            return
        if event.document_id != editor.w.id or event.rc_model_digest != editor.profile.rc_model_digest:
            self._cancel_all(clear=True)
            return
        target = tuple(sorted(event.changed))
        self.submit(target, baseline_workspace=event.before, baseline_target_uids=target)
        self.request_key = self._key(target)

    def submit(self, target_uids, *, extra_target=GeometrySet(), exclude_uids=(),
               baseline_workspace=None, baseline_target_uids=None, scope=None,
               candidate=False, source_workspace=None):
        editor = self.window.editor
        if not editor or not self.enabled or self.dock.isHidden():
            return
        self.generation += 1
        if baseline_workspace is not None and baseline_target_uids is None:
            baseline_target_uids = target_uids
        request = AnalysisRequest(
            editor, editor.w, editor.profile, editor.scope_id if scope is None else scope,
            self.window.canvas.blackbox_peek > 0, target_uids, extra_target, tuple(exclude_uids),
            baseline_workspace, baseline_target_uids, source_workspace, candidate,
            RcBudget(analysis_radius_um=self.panel.radius.value()), self.generation, monotonic(), Event(),
        )
        self.pending = request
        self.estimate = None
        self.panel.status.setText('本地计算排队中…')
        self._sample_light()
        self._sample_heat()

    def preview_candidate(self, workspace, target_uids, baseline_workspace, *, scope=None, source='human'):
        self.candidate_source = source
        self.submit(target_uids, baseline_workspace=baseline_workspace, baseline_target_uids=(),
                    scope=scope, candidate=True, source_workspace=baseline_workspace)
        self.request_key = None

    def cancel_candidate(self, source=None):
        if self.candidate_source is None or source is not None and self.candidate_source != source:
            return
        self.candidate_source = None
        self._cancel_all(clear=True)
        self.refresh()

    def agent_route_event(self, event):
        if event.action == 'discard':
            self.cancel_candidate('agent')
            return
        if not self.enabled or self.dock.isHidden() or event.editor is not self.window.editor:
            return
        if event.baseline is not self.window.editor.w or event.scope_id != self.window.editor.scope_id:
            return
        self.preview_candidate(event.candidate, event.target_uids, event.baseline,
                               scope=event.scope_id, source='agent')

    def begin_draft(self, workspace):
        self.gesture_baseline = workspace

    def preview_draft(self, geometry):
        if self.gesture_baseline is None:
            return
        self.submit((), extra_target=geometry, baseline_workspace=self.gesture_baseline,
                    baseline_target_uids=())

    def cancel_draft_preview(self):
        if self.gesture_baseline is None:
            return
        self.generation += 1
        for request in (self.active, self.pending):
            if request:
                request.cancelled.set()
        self.pending = None
        self.estimate = None
        self.panel.status.setText('等待走线预览…')
        self.window.canvas.viewport().update()

    def cancel_draft(self):
        self.gesture_baseline = None
        self._cancel_all(clear=True)

    def finish_draft(self):
        self.gesture_baseline = None
        human = getattr(self.window, 'human', None)
        ids = tuple(sorted(human.ids())) if human else ()
        if self._key(ids or None) != self.request_key:
            self.request_key = None
            self.refresh()

    def begin_transform(self, uids, workspace):
        self.gesture_baseline = workspace

    def preview_transform(self, uids, delta):
        if self.gesture_baseline is None:
            return
        rows = []
        objects = projected(self.gesture_baseline)
        for uid in uids:
            obj = objects.get(uid)
            if not obj or not obj.shape:
                continue
            shape = transform_shape(obj.shape, delta=delta)
            rows.append(shape_geometry(shape, self.window.editor.profile, uid, component_id=f'move:{uid}', simple_series=True))
        geometry = merge_geometry_sets(*rows) if rows else GeometrySet()
        geometry = GeometrySet(geometry.conductors, geometry.resistances, len(uids) == 1)
        self.submit(tuple(uids), extra_target=geometry, exclude_uids=tuple(uids),
                    baseline_workspace=self.gesture_baseline, baseline_target_uids=tuple(uids))

    def finish_transform(self):
        self.gesture_baseline = None
        self.request_key = None
        self.refresh()

    def cancel_transform(self):
        if self.gesture_baseline is None:
            return
        self.gesture_baseline = None
        self._cancel_all(clear=True)

    def _sample_light(self):
        if self.closed:
            return
        request = self.pending or self.active
        if not request:
            return
        value = None
        status = 'unavailable'
        try:
            if request.extra_target.conductors:
                rows = request.extra_target
            elif request.target_uids and len(request.target_uids) == 1:
                uid = request.target_uids[0]
                obj = projected(request.workspace).get(uid)
                rows = shape_geometry(obj.shape, request.profile, uid) if obj and obj.shape else GeometrySet(simple_series=False)
            else:
                rows = GeometrySet(simple_series=False)
            values = [row.ohms for row in rows.resistances]
            if rows.simple_series and values and all(value is not None for value in values):
                value = sum(values)
                status = 'complete'
            self.panel.set_resistance(value, status)
        except (KeyError, TypeError, ValueError):
            self.panel.set_resistance(None, 'unavailable')

    def _sample_heat(self):
        if self.closed or not self.enabled:
            return
        if self.future is not None:
            if self.pending and monotonic() - self.pending.requested > .25 and self.active:
                self.active.cancelled.set()
            return
        if self.pending is None:
            return
        self.active = self.pending
        self.pending = None
        request = self.active
        self.future = self.executor.submit(self._execute, request)
        self._poll_timer.start()

    @staticmethod
    def _execute(request):
        estimate, _ = analyze_workspace(
            request.workspace, request.profile, request.scope, request.target_uids,
            peek=request.peek, extra_target=request.extra_target, exclude_uids=request.exclude_uids,
            budget=request.budget, cancelled=request.cancelled,
        )
        delta = None
        if request.baseline_workspace is not None:
            before, _ = analyze_workspace(
                request.baseline_workspace, request.profile, request.scope, request.baseline_target_uids,
                peek=request.peek, budget=request.budget, cancelled=request.cancelled,
            )
            delta = compare_estimates(before, estimate)
        return AnalysisOutcome(estimate, delta, request.baseline_workspace is not None)

    def _poll(self):
        if self.future is None or not self.future.done():
            return
        future = self.future
        self.future = None
        request = self.active
        self.active = None
        try:
            outcome = future.result()
        except RcCancelled:
            outcome = None
        except Exception as exc:
            outcome = None
            if request.generation == self.generation:
                self.panel.status.setText('RC 计算未完成：' + str(exc))
        current = self.window.editor.w is request.source_workspace if request.candidate else self.window.editor.w is request.workspace
        if outcome is not None and not self.closed and request.generation == self.generation \
                and self.window.editor is request.editor and current:
            self.estimate = outcome.estimate
            self.estimate_key = request.generation
            self.panel.set_estimate(outcome)
            self.window.canvas.viewport().update()
        if self.pending is not None:
            self._sample_heat()

    def paint(self, painter):
        estimate = self.estimate
        if self.closed or estimate is None or self.dock.isHidden():
            return
        colors = palette(self.window.canvas)
        painter.save()
        try:
            painter.setTransform(self.window.canvas.viewportTransform())
            for impact in estimate.impacts:
                if impact.target_internal or impact.status != 'complete' or not impact.capacitance_fF:
                    continue
                color = QColor('#ed9490' if impact.relation == 'different_net' else
                               '#86b9ed' if impact.relation == 'same_net' else '#a7a9ad')
                painter.setPen(QPen(color, 1.2))
                painter.setBrush(alpha(color, min(.65, .12 + impact.capacitance_fF / 20)))
                painter.drawPath(polygon_path(impact.geometry))
            painter.resetTransform()
            badge = QRectF(12, 44, 285, 25)
            painter.fillRect(badge, QColor(colors.net_badge))
            painter.setPen(QColor(colors.local_label))
            prefix = 'RC 神经网络估算 · 非签核' if getattr(estimate, 'mode', 'analytic') != 'analytic' else 'RC 本地估算 · 非签核'
            painter.drawText(badge, Qt.AlignmentFlag.AlignVCenter,
                             prefix + ('' if estimate.comparable else ' · 部分覆盖'))
        finally:
            painter.restore()
