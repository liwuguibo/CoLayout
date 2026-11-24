"""User-triggered communication controller over the existing Adapter.

Owns UI preparation/completion only; native checks and transactions remain
in Adapter. Calling a drawing or selection operation never invokes this class.
"""


class SyncController:
    def __init__(self, window):
        self.window = window

    def pull(self, checked=False, *, fresh=False):
        host = self.window
        if fresh and not host.idle_for_replacement():
            return
        if getattr(host, 'document_busy', lambda: host.busy)():
            return
        old = host.editor.w if host.editor else None
        if old and not fresh and old.state in ('submitting', 'indeterminate', 'conflict'):
            if old.state in ('submitting', 'indeterminate'):
                from ..sync_recovery import recovery_message

                host.error((recovery_message(old), '当前工作区未修改。'))
            else:
                host.error(
                    (
                        '当前工作区处于冲突状态。请先核对或从“文件 → 从 Virtuoso 重新导入”开始新的工作区。',
                        '刷新未执行，当前工作区未修改。',
                    )
                )
            return
        backup = None
        if old:
            from ..sync_recovery import backup as save_backup

            backup = save_backup(host, 'before-import')
        lib, cell = host.library.text().strip(), host.cell.text().strip()

        def load():
            import time
            from datetime import datetime, timezone
            from uuid import uuid4
            from ..import_performance import save_report

            start = time.perf_counter()
            a = None
            report = {
                'library': lib,
                'cell': cell,
                'fresh': fresh,
                'started_at': datetime.now(timezone.utc).isoformat(),
            }
            try:
                a = host.native()
                connected = time.perf_counter()
                report['connection_wait_s'] = connected - start
                if not fresh and old and (lib, cell) == (old.baseline.library, old.baseline.cell):
                    if old.operations or old.local_data.regions:
                        result = a.cooperative_pull(old)
                    else:
                        result = a.refresh(old)
                else:
                    result = a.open_workspace(lib, cell)
                report.update(status='success', timings=getattr(a, 'last_read_timings', {}))
                return result
            except Exception as exc:
                report.update(status='error', error=str(exc))
                raise
            finally:
                report['total_s'] = time.perf_counter() - start
                native = getattr(a, 'native', None)
                if native is not None and hasattr(native, 'state'):
                    save_report(
                        native.state / 'timings' / ('import-' + uuid4().hex + '.json'),
                        report,
                    )

        def done(result):
            if fresh or not old or (lib, cell) != (old.baseline.library, old.baseline.cell):
                if fresh:
                    host.install(*result)
                else:
                    host.install_preserving_view(*result)
                host.save_local()
                host.status.setText(
                    (
                        '已重新导入服务器版图和原理图；旧工作区已备份。'
                        if fresh
                        else '已刷新服务器数据，并保留本地组织。'
                    )
                    + (' 备份：' + str(backup) if backup else '')
                )
                return
            merged_w, merged_p = result
            host.workspace_controller.install_cooperative(merged_w, merged_p)
            delta = (merged_w.last_result or {}).get('virtuoso_delta') or {}
            delta_total = sum(delta.get(k, 0) for k in ('added', 'modified', 'deleted'))
            pending = len(merged_w.operations)
            if delta_total and pending:
                msg = f'已合并 Virtuoso 区外改动（{delta_total} 项）并保留本地草稿（{pending} 项）。'
            elif pending:
                msg = f'已刷新 Virtuoso 基准并保留本地操作区草稿（{pending} 项）。'
            elif delta_total:
                msg = f'已从 Virtuoso 合并 {delta_total} 项改动。'
            else:
                msg = '已读取服务器数据；当前与 Virtuoso 一致。'
            host.status.setText(msg + (' 备份：' + str(backup) if backup else ''))

        host.communication_action = 'refresh'
        host.communication_task(load, done)

    def sync(self):
        host = self.window
        if not host.editor:
            return
        if getattr(host, 'document_busy', lambda: host.busy)():
            return
        w = host.editor.w
        p = host.editor.profile
        active_regions = {s.region_id for s in host.editor.work_sessions.snapshots()} or None

        def done(result):
            host.workspace_controller.install_cooperative(result, p)
            status = result.last_result.get('status')
            if status == 'saved_verified':
                remaining = len(result.operations)
                if remaining:
                    host.status.setText(
                        f'已完成非占用区域的协同写入并通过读回核对（保留进行中操作区 {remaining} 项草稿） · '
                        + result.baseline.cell
                    )
                    host.diagnostics.set_result(
                        '已完成非占用区域协同写入，服务器读回核对通过。\n目标：'
                        + result.baseline.library
                        + '/'
                        + result.baseline.cell
                        + f'\n仍在工作中的操作区草稿保留：{remaining} 项。'
                    )
                else:
                    host.status.setText('同步已完成并通过读回核对 · ' + result.baseline.cell)
                    host.diagnostics.set_result(
                        '同步已完成，服务器读回核对通过。\n目标：'
                        + result.baseline.library
                        + '/'
                        + result.baseline.cell
                        + '\n无待同步操作。'
                    )
            elif status == 'rolled_back':
                host.status.setText(
                    '同步失败，目标已恢复；本地草稿保留。失败操作：'
                    + str(result.last_result.get('failed_operation'))
                )
            elif status == 'no_op':
                if active_regions and w.operations:
                    host.status.setText('当前待同步改动均位于 Agent 占用中的操作区，请先释放后再推送。')
                else:
                    host.status.setText('没有待同步的本地修改。')

        host.communication_task(
            lambda: host.native().commit(w, exclude_region_ids=active_regions), done
        )

    def reconcile(self):
        host = self.window
        if not host.idle_for_replacement():
            return
        if not host.editor or host.busy:
            return
        from ..sync_recovery import backup, persisted_pending

        saved = backup(host, 'before-reconcile')
        source = persisted_pending(host.editor.w) or host.editor.w

        def done(result):
            host.install_preserving_view(result, host.editor.profile)
            host.save_local()
            host.status.setText(
                (
                    '已核对：上次同步成功，已恢复正常编辑。'
                    if result.state == 'clean'
                    else '已核对：上次修改未应用或已回滚，本地草稿保留。'
                )
                + ' 备份：'
                + str(saved)
            )

        host.communication_task(lambda: host.native().reconcile(source), done)

    def open_native(self):
        host = self.window
        if host.editor:
            s = host.editor.w.baseline
            host.task(
                lambda: host.native().native.call('ltOpen', s.library, s.cell),
                lambda _: host.status.setText('已打开对应 Virtuoso Layout：' + s.cell),
            )
