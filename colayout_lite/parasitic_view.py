"""On-demand local RC estimates and disposable canvas overlays (zero unsolicited work)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from time import monotonic

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .parasitic_rc import RcBudget, RcCancelled
from .parasitic_workspace import evaluate_parasitics_outcome
from .theme import alpha, palette


@dataclass
class AnalysisRequest:
    editor: object
    workspace: object
    profile: object
    scope: object
    peek: bool
    target_uids: object
    net: str | None
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
    selection: dict | None = None


def _selection_summary(selection: dict | None, *, candidate: bool = False) -> str:
    if not selection:
        return '—'
    basis = selection.get('basis', 'region')
    nets = selection.get('nets') or ()
    target_uids = selection.get('target_uids')
    if basis == 'net' and nets:
        text = f"网络 {', '.join(nets)}"
    elif basis == 'uids' and target_uids is not None:
        text = f'指定对象 ({len(target_uids)})'
        if nets:
            text += f" · {', '.join(nets)}"
    elif basis == 'bounds':
        text = '指定范围'
    else:
        text = '当前操作区'
    return f'布线候选 · {text}' if candidate else text


class RcPanel(QWidget):
    def __init__(self, owner):
        super().__init__(owner.window)
        self.owner = owner
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.evaluate_button = QPushButton('计算当前 RC')
        self.evaluate_button.setObjectName('rcEvaluate')
        self.evaluate_button.setToolTip(
            '按需使用本地冻结几何和已审核参数计算所选导线/网络（或最近一次走线）的 RC；不替代 Calibre 签核提取。'
        )
        self.evaluate_button.clicked.connect(lambda: owner.evaluate_now())
        self.clear_button = QPushButton('清除')
        self.clear_button.setObjectName('rcClear')
        self.clear_button.clicked.connect(owner.clear_result)
        actions.addWidget(self.evaluate_button, 1)
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)

        form = QFormLayout()
        self.target = QLabel('—')
        self.resistance = QLabel('—')
        self.ground = QLabel('—')
        self.coupling = QLabel('—')
        self.delta = QLabel('—')
        for label in (self.target, self.resistance, self.ground, self.coupling, self.delta):
            label.setMinimumWidth(0)
        form.addRow('分析目标', self.target)
        form.addRow('R', self.resistance)
        form.addRow('Cg · 参考地', self.ground)
        form.addRow('Cc · 邻近耦合', self.coupling)
        form.addRow('ΔR / ΔCg / ΔCc', self.delta)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(0.1, 100)
        self.radius.setValue(10)
        self.radius.setSuffix(' µm')
        self.radius.setSingleStep(0.5)
        self.radius.valueChanged.connect(owner.set_radius)
        form.addRow('近邻范围', self.radius)
        layout.addLayout(form)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        layout.addWidget(self.status)
        self.details = QLabel(
            '仅按需计算，日常编辑零负担。\nCg 不含显式邻近耦合；每对只计一次。'
        )
        self.details.setWordWrap(True)
        self.details.setMinimumWidth(0)
        layout.addWidget(self.details)
        layout.addStretch(1)

    @staticmethod
    def number(value, unit):
        return '—' if value is None else f'{value:.6g} {unit}'

    def set_resistance(self, value, status='complete'):
        self.resistance.setText(self.number(value, 'Ω') + ('' if status == 'complete' else f' · {status}'))

    def clear_values(self):
        self.target.setText('—')
        self.resistance.setText('—')
        self.ground.setText('—')
        self.coupling.setText('—')
        self.delta.setText('—')

    def set_estimate(self, outcome: AnalysisOutcome, *, candidate: bool = False):
        estimate = outcome.estimate
        self.target.setText(_selection_summary(outcome.selection, candidate=candidate))
        self.set_resistance(estimate.resistance_ohm, estimate.resistance_status)
        self.ground.setText(self.number(estimate.ground_capacitance_fF, 'fF') + ' · ' + estimate.ground_status)
        self.coupling.setText(self.number(estimate.coupling_capacitance_fF, 'fF') + ' · ' + estimate.coupling_status)
        delta = outcome.delta
        if not outcome.has_baseline:
            self.delta.setText('—')
        else:
            self.delta.setText(
                '覆盖不一致，不能直接比较总量'
                if delta is None
                else f'{delta["delta_r_ohm"]:+.4g} Ω / {delta["delta_cg_fF"]:+.4g} fF / {delta["delta_cc_fF"]:+.4g} fF'
                if delta['delta_r_ohm'] is not None
                else f'— / {delta["delta_cg_fF"]:+.4g} fF / {delta["delta_cc_fF"]:+.4g} fF'
            )
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
        self.enabled = bool(window.editor and window.editor.profile.parasitics)
        self.closed = False
        self.bound_editor = None
        self.commit_unsubscribe = None
        self.last_commit = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(20)
        self._poll_timer.timeout.connect(self._poll)
        window.canvas.parasitic_overlay = self.paint
        window.view_state_changed.connect(self.refresh)
        self.dock.visibilityChanged.connect(self._visibility)
        self.dock.setVisible(False)
        self.refresh()

    def _visibility(self, visible):
        if not visible:
            self._cancel_all(clear=True)
        self.refresh()
        self.window.view_state_changed.emit()

    def set_radius(self, value):
        del value
        if self.estimate is not None and not self.closed:
            self.panel.status.setText('近邻范围已调整 · 点击“计算当前 RC”更新')

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

    def clear_result(self):
        self._cancel_all(clear=True)
        self.refresh()

    def _cancel_all(self, clear=False):
        self.generation += 1
        for request in (self.active, self.pending):
            if request:
                request.cancelled.set()
        self.pending = None
        if clear:
            self._poll_timer.stop()
            self.estimate = None
            self.estimate_key = None
            self.panel.clear_values()
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
        self.last_commit = None
        self._poll_timer.stop()
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
        self.last_commit = None
        self.commit_unsubscribe = editor.subscribe_commits(self.commit) if editor else None

    def refresh(self):
        if self.closed:
            return
        self._bind_editor()
        editor = self.window.editor
        configured = bool(editor and editor.profile.parasitics)
        self.enabled = configured
        self.panel.evaluate_button.setEnabled(configured)
        self.panel.clear_button.setEnabled(
            configured and (self.estimate is not None or self.future is not None or self.pending is not None)
        )
        self.panel.radius.setEnabled(configured)
        if not configured:
            if self.estimate is not None or self.future is not None or self.pending is not None:
                self._cancel_all(clear=True)
            self.panel.status.setText('请在工艺库配置并审核 RC 参数。')
            return
        if self.estimate is None and self.future is None and self.pending is None:
            self.panel.status.setText('按需估算 · 选定后点击“计算当前 RC”')

    def commit(self, event):
        editor = self.window.editor
        if self.closed or not editor or editor.profile is not event.profile:
            return
        if event.document_id != editor.w.id or event.rc_model_digest != editor.profile.rc_model_digest:
            self.last_commit = None
            self._cancel_all(clear=True)
            return
        self.last_commit = event
        if self.estimate is not None:
            self.panel.status.setText('版图已更新（未自动重算）· 点击“计算当前 RC”更新')

    def _resolve_target(self, editor, target_uids, net, workspace, baseline_workspace, baseline_target_uids):
        candidate = False
        source_workspace = None
        inspection = getattr(self.window, 'inspection', None)
        if target_uids is None and net is None:
            if (
                workspace is None
                and inspection is not None
                and getattr(inspection, 'preview', None) is not None
                and inspection.preview.current(editor)
            ):
                preview = inspection.preview
                workspace = preview.workspace
                source_workspace = preview.source
                baseline_workspace = preview.source
                target_uids = tuple(
                    op.uid for op in workspace.operations[len(preview.source.operations):]
                )
                baseline_target_uids = ()
                candidate = True
            else:
                human = getattr(self.window, 'human', None)
                ids = tuple(sorted(human.ids())) if human else ()
                if ids:
                    target_uids = ids
                    if (
                        baseline_workspace is None
                        and self.last_commit is not None
                        and self.last_commit.after is editor.w
                        and set(ids) == set(self.last_commit.changed)
                    ):
                        baseline_workspace = self.last_commit.before
                        baseline_target_uids = ids
                elif inspection is not None and getattr(inspection, 'trace', None):
                    nets = tuple(sorted(inspection.trace.get('nets', ())))
                    if len(nets) == 1:
                        net = nets[0]
                    elif getattr(inspection, 'highlighted_owners', None):
                        target_uids = tuple(sorted(inspection.highlighted_owners))
                elif (
                    self.last_commit is not None
                    and self.last_commit.after is editor.w
                    and self.last_commit.changed
                ):
                    target_uids = tuple(sorted(self.last_commit.changed))
                    if baseline_workspace is None:
                        baseline_workspace = self.last_commit.before
                        baseline_target_uids = target_uids
        if baseline_workspace is not None and baseline_target_uids is None:
            baseline_target_uids = target_uids if target_uids is not None else ()
        return (
            workspace or editor.w,
            target_uids,
            net,
            baseline_workspace,
            baseline_target_uids,
            source_workspace,
            candidate,
        )

    def evaluate_now(
        self,
        target_uids=None,
        *,
        net: str | None = None,
        baseline_workspace=None,
        baseline_target_uids=None,
        workspace=None,
        scope=None,
    ) -> bool:
        """Run one explicit on-demand RC estimation using the shared domain service."""
        if self.closed:
            return False
        self._bind_editor()
        editor = self.window.editor
        if not editor or not editor.profile.parasitics:
            self.panel.status.setText('请在工艺库配置并审核 RC 参数。')
            return False
        (
            ws,
            resolved_uids,
            resolved_net,
            resolved_baseline,
            resolved_baseline_uids,
            source_workspace,
            candidate,
        ) = self._resolve_target(
            editor,
            target_uids,
            net,
            workspace,
            baseline_workspace,
            baseline_target_uids,
        )
        self.generation += 1
        if self.active is not None:
            self.active.cancelled.set()
        request = AnalysisRequest(
            editor=editor,
            workspace=ws,
            profile=editor.profile,
            scope=editor.scope_id if scope is None else scope,
            peek=self.window.canvas.blackbox_peek > 0,
            target_uids=resolved_uids,
            net=resolved_net,
            baseline_workspace=resolved_baseline,
            baseline_target_uids=resolved_baseline_uids,
            source_workspace=source_workspace,
            candidate=candidate,
            budget=RcBudget(analysis_radius_um=self.panel.radius.value(), time_seconds=2.0),
            generation=self.generation,
            requested=monotonic(),
            cancelled=Event(),
        )
        self.pending = request
        self.estimate = None
        self.panel.status.setText('正在按需计算 RC…')
        self.panel.clear_button.setEnabled(True)
        self._dispatch_pending()
        return True

    def _dispatch_pending(self):
        if self.closed or self.pending is None:
            return
        if self.future is not None:
            if not self._poll_timer.isActive():
                self._poll_timer.start()
            return
        self.active = self.pending
        self.pending = None
        self.future = self.executor.submit(self._execute, self.active)
        self._poll_timer.start()

    @staticmethod
    def _execute(request: AnalysisRequest) -> AnalysisOutcome:
        estimate, delta, has_baseline, selection = evaluate_parasitics_outcome(
            request.workspace,
            request.profile,
            request.scope,
            uids=request.target_uids,
            net=request.net,
            analysis_radius_um=request.budget.analysis_radius_um,
            peek=request.peek,
            baseline_workspace=request.baseline_workspace,
            baseline_uids=request.baseline_target_uids,
            budget=request.budget,
            cancelled=request.cancelled,
        )
        return AnalysisOutcome(estimate, delta, has_baseline, selection)

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
        editor = self.window.editor
        current = bool(
            editor
            and request.editor is editor
            and (
                editor.w is request.source_workspace
                if request.candidate
                else editor.w is request.workspace
            )
        )
        if outcome is not None and not self.closed and request.generation == self.generation and current:
            self.estimate = outcome.estimate
            self.estimate_key = request.generation
            self.panel.set_estimate(outcome, candidate=request.candidate)
            self.panel.clear_button.setEnabled(True)
            self.window.canvas.viewport().update()
        if self.pending is not None:
            self._dispatch_pending()
        elif self.future is None:
            self._poll_timer.stop()

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
                color = QColor(
                    '#ed9490'
                    if impact.relation == 'different_net'
                    else '#86b9ed'
                    if impact.relation == 'same_net'
                    else '#a7a9ad'
                )
                painter.setPen(QPen(color, 1.2))
                painter.setBrush(alpha(color, min(0.65, 0.12 + impact.capacitance_fF / 20)))
                painter.drawPath(polygon_path(impact.geometry))
            painter.resetTransform()
            badge = QRectF(12, 44, 285, 25)
            painter.fillRect(badge, QColor(colors.net_badge))
            painter.setPen(QColor(colors.local_label))
            prefix = (
                'RC 神经网络估算 · 非签核'
                if getattr(estimate, 'mode', 'analytic') != 'analytic'
                else 'RC 本地估算 · 非签核'
            )
            painter.drawText(
                badge,
                Qt.AlignmentFlag.AlignVCenter,
                prefix + ('' if estimate.comparable else ' · 部分覆盖'),
            )
        finally:
            painter.restore()


def polygon_path(geometry):
    from .inspection_view import polygon_path as _polygon_path

    return _polygon_path(geometry)
