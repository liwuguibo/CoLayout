"""Shared saved-workspace use cases with explicit file roots and no native calls."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .document import load_document, save_document
from .model import LiteError
from .transactions import write_json_atomic


class WorkspaceFiles:
    def __init__(self, editor, current_path, open_document, folder, *, import_roots=(),
                 busy=lambda: False, save_busy=None, path_warning=lambda: None,
                 storage=None, import_files=lambda: (), native=None,
                 remote_enabled=lambda: True, install_cooperative=None,
                 save_local=None, on_cooperative_baseline=None,
                 on_guides_synced=None):
        from .workspace_storage import WorkspaceStorage
        self.storage = storage or WorkspaceStorage(Path(folder) / '.runtime')
        self.import_files = import_files
        self.editor = editor
        self.current_path = current_path
        self.open_document = open_document
        self.folder = Path(folder).resolve()
        self.import_roots = tuple(dict.fromkeys([
            self.folder, self.storage.folder/'backups', *(Path(p).resolve() for p in import_roots)]))
        self.busy = busy
        self.save_busy = save_busy or busy
        self.path_warning = path_warning
        self.native = native
        self.remote_enabled = remote_enabled
        self.install_cooperative = install_cooperative
        self.save_local = save_local
        self.on_cooperative_baseline = on_cooperative_baseline
        self.on_guides_synced = on_guides_synced

    def status(self):
        e = self.editor()
        scale = e.profile.dbu_per_micron
        return {'document_id':e.w.id, 'revision':e.w.revision, 'state':e.w.state,
                'path':str(self.current_path()), 'library':e.w.baseline.library,
                'cell':e.w.baseline.cell, 'pending_operations':len(e.w.operations),
                'active_regions':len(e.work_sessions.snapshots()),
                'regions':[
                    {
                        'id': r.id,
                        'name': r.name,
                        'parent_id': r.parent_id,
                        'pin_mode': r.pin_mode,
                        'bounds_um': [v / scale for p in r.bbox for v in p],
                    }
                    for r in e.w.local_data.regions.values()
                ],
                'archive_folder':str(self.folder),
                'import_roots':[str(p) for p in self.import_roots],
                'import_files':[str(Path(p).resolve()) for p in self.import_files() if p],
                'can_load':not self.busy() and not e.work_sessions.snapshots()
                    and e.w.state not in ('submitting','indeterminate','conflict')}

    def expected(self, args):
        e = self.editor()
        if args.expected_document_id != e.w.id:
            raise LiteError('WORKSPACE_CHANGED','请先读取 workspace status；当前文档已改变。')
        if type(args.expected_revision) is not int or args.expected_revision != e.w.revision:
            raise LiteError('STALE_REVISION','请先读取当前工作区版本。')
        return e

    def _save(self, e, name):
        name = (name or e.w.baseline.cell).strip()
        if not name or len(name)>100 or re.search(r'[<>:"/\\|?*\x00-\x1f]',name):
            raise LiteError('INVALID_WORKSPACE_NAME','name 是存档名称，不是目录；不要包含路径分隔符。')
        self.folder.mkdir(parents=True,exist_ok=True)
        stem=f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{name}_r{e.w.revision}_{uuid4().hex[:8]}'
        path=self.folder/(stem+'.colayout.json')
        save_document(path,e.w,e.profile)
        row={'path':str(path),'name':name,'document_id':e.w.id,'revision':e.w.revision,
             'created_at_utc':datetime.now(timezone.utc).isoformat(),
             'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
             'pending_operations':len(e.w.operations),'regions':len(e.w.local_data.regions)}
        try:
            index=self.storage.folder/'archive-index'/hashlib.sha256(str(path).encode()).hexdigest()
            write_json_atomic(index.with_suffix('.meta.json'),row)
        except OSError as exc:
            row['save_warning']='工作区文件已保存，但目录索引写入失败：'+str(exc)
        return row

    def input_path(self, value):
        if not value:
            raise LiteError('WORKSPACE_FILE_REQUIRED')
        path=Path(value)
        path=(path if path.is_absolute() else self.folder/path).resolve()
        if (path not in {Path(p).resolve() for p in self.import_files() if p}
                and not any(path.is_relative_to(root) for root in self.import_roots)):
            raise LiteError('WORKSPACE_FILE_OUTSIDE_ROOT','文件须位于 workspace status 的 import_roots，或属于 import_files 中明确选定的文件。')
        if path.suffix.lower()!='.json' or not path.is_file():
            raise LiteError('WORKSPACE_FILE_REQUIRED','请选择已保存的 .colayout.json 文件。')
        if path.stat().st_size>256*1024*1024:
            raise LiteError('WORKSPACE_FILE_TOO_LARGE','单个工作区文件上限为 256 MiB。')
        return path

    def sync_guides_best_effort(self, w):
        if self.native is None or not self.remote_enabled():
            return {'synced': False, 'reason': 'unavailable'}
        try:
            reply = self.native().sync_region_guides(w)
            if self.on_guides_synced is not None:
                self.on_guides_synced(w)
            return reply
        except Exception as exc:
            return {'synced': False, 'warning': str(exc)}

    def _adopt_merged(self, e, workspace, profile):
        if self.install_cooperative is not None:
            self.install_cooperative(workspace, profile)
        else:
            e.adopt_cooperative_baseline(workspace, profile)
            if self.on_cooperative_baseline is not None:
                self.on_cooperative_baseline()
            if self.save_local is not None:
                self.save_local()

    def sync_to_virtuoso(self, *, region_id=None):
        e = self.editor()
        if self.busy():
            raise LiteError('WORKSPACE_BUSY', '等待当前原生/文档任务结束后再推送。')
        if not self.remote_enabled() or self.native is None:
            raise LiteError('REMOTE_UNAVAILABLE', '当前窗口未启用远端 Virtuoso 连接。')
        if e.w.state in ('submitting', 'indeterminate', 'conflict'):
            raise LiteError('WORKSPACE_REQUIRES_RECOVERY', '工作区有未决或冲突状态，请先在人端核对。')
        from .regions.query import descendants

        active_regions = {s.region_id for s in e.work_sessions.snapshots()}
        if region_id is not None:
            if region_id not in e.w.local_data.regions:
                raise LiteError('REGION_MISSING', region_id)
            subtree = descendants(e.w, region_id)
            if subtree & active_regions:
                raise LiteError(
                    'WORK_SESSIONS_ACTIVE',
                    f'操作区 {region_id}（或其子区）正在被 Agent 占用，请先 release_region 后再推送。',
                )
            committed = self.native().commit(e.w, scope_region_ids={region_id})
        else:
            committed = self.native().commit(e.w, exclude_region_ids=active_regions or None)
        self._adopt_merged(e, committed, e.profile)
        return {'synced': True, 'region_id': region_id, 'last_result': committed.last_result, **self.status()}

    def invoke(self, args):
        if args.action=='status':
            return self.status()
        if args.action=='list':
            rows={}
            # Legacy sidecar indexes remain readable; new ones stay internal.
            for index in (self.folder,self.storage.folder/'archive-index'):
                for file in index.glob('*.meta.json'):
                    row=json.loads(file.read_text(encoding='utf8'))
                    if Path(row['path']).resolve().parent==self.folder and Path(row['path']).is_file():
                        rows[row['path']]=row
            rows=sorted(rows.values(),key=lambda r:r['created_at_utc'],reverse=True)
            return {'files':rows[args.offset:args.offset+args.limit], 'total':len(rows)}
        if args.action=='sync_regions':
            e=self.editor()
            if self.busy():
                raise LiteError('WORKSPACE_BUSY','等待当前原生/文档任务结束后再推送区域边界。')
            if not self.remote_enabled() or self.native is None:
                raise LiteError('REMOTE_UNAVAILABLE','当前窗口未启用远端 Virtuoso 连接。')
            reply=self.native().sync_region_guides(e.w)
            if self.on_guides_synced is not None:
                self.on_guides_synced(e.w)
            return {'guides_synced':True,**reply,**self.status()}
        e=self.expected(args)
        if args.action=='save':
            if self.save_busy():
                raise LiteError('WORKSPACE_BUSY','等待当前原生/文档任务结束后保存存档。')
            return {'saved':True,'active_document_changed':False,**self._save(e,args.name)}
        if args.action=='create_region':
            if self.busy():
                raise LiteError('WORKSPACE_BUSY','等待当前原生/文档任务结束后再创建操作区。')
            from .actions import dbu
            from .regions.actions import RegionActions

            bounds=tuple(dbu(v,e.profile) for v in args.bounds_um)
            actions=RegionActions(e)
            old_scope=e.scope_id
            switched=False
            if not e.work_sessions._actor and e.scope_id != args.parent_id:
                e.scope_id=args.parent_id
                switched=True
            try:
                rid=actions.create(bounds,name=args.name or '',parent_id=args.parent_id)
                if args.pin_mode is not None:
                    actions.set_pin_mode(rid,args.pin_mode)
                if args.note is not None or args.blocked_layers is not None:
                    reg=e.w.local_data.regions[rid]
                    actions.update_details(
                        rid,
                        reg.name,
                        args.note if args.note is not None else reg.note,
                        tuple(args.blocked_layers) if args.blocked_layers is not None else reg.blocked_layers,
                    )
            finally:
                if switched:
                    e.scope_id=old_scope
            if self.save_local is not None:
                self.save_local()
            guide_res=self.sync_guides_best_effort(e.w)
            return {'created':True,'region_id':rid,'guides_sync':guide_res,**self.status()}
        if args.action=='dissolve_region':
            if self.busy():
                raise LiteError('WORKSPACE_BUSY','等待当前原生/文档任务结束后再解散操作区。')
            from .regions.actions import RegionActions

            reg=e.w.local_data.regions.get(args.region_id)
            if reg is None:
                raise LiteError('REGION_MISSING',args.region_id)
            old_scope=e.scope_id
            switched=False
            if not e.work_sessions._actor and e.scope_id != reg.parent_id:
                e.scope_id=reg.parent_id
                switched=True
            try:
                RegionActions(e).dissolve(args.region_id)
            finally:
                if switched:
                    e.scope_id=old_scope if old_scope in e.w.local_data.regions else reg.parent_id
            if self.save_local is not None:
                self.save_local()
            guide_res=self.sync_guides_best_effort(e.w)
            return {'dissolved':args.region_id,'guides_sync':guide_res,**self.status()}
        if args.action=='pull':
            if self.busy():
                raise LiteError('WORKSPACE_BUSY','等待当前原生/文档任务结束后再拉取。')
            if not self.remote_enabled() or self.native is None:
                raise LiteError('REMOTE_UNAVAILABLE','当前窗口未启用远端 Virtuoso 连接。')
            if e.w.state in ('submitting','indeterminate','conflict'):
                raise LiteError('WORKSPACE_REQUIRES_RECOVERY','工作区有未决或冲突状态，请先在人端核对。')
            merged_w,profile=self.native().cooperative_pull(e.w)
            self._adopt_merged(e,merged_w,profile)
            return {'pulled':True,'last_result':merged_w.last_result,**self.status()}
        if args.action=='sync':
            return self.sync_to_virtuoso(region_id=args.region_id)
        e.check_revision(args.expected_revision)
        e.work_sessions.require_idle()
        if self.busy():
            raise LiteError('WORKSPACE_BUSY','加载前请结束所有后台任务。')
        path=self.input_path(args.path)
        raw=path.read_bytes()
        try:
            payload=json.loads(raw)
        except (ValueError,UnicodeError) as exc:
            raise LiteError('WORKSPACE_FILE_INVALID','不是有效的工作区 JSON 文件。') from exc
        if not isinstance(payload,dict) or payload.get('schema_version')!='colayout-document-1':
            raise LiteError('WORKSPACE_FILE_INVALID','不是 CoLayout 工作区文件。')
        # Validation/migrations operate on a new working file, never on the archive.
        working,_=self.storage.prepare(path)
        loaded,profile=load_document(working)
        from .sync_recovery import restore_pending_state
        loaded=restore_pending_state(loaded)
        if loaded.state in ('submitting','indeterminate','conflict'):
            raise LiteError('WORKSPACE_REQUIRES_RECOVERY','该存档有未决原生状态，需由维护者先核对回执。')
        saved=self.storage.backup(e.w,e.profile,'before-load')
        backup={'path':str(saved),'document_id':e.w.id,'revision':e.w.revision,
                'sha256':hashlib.sha256(saved.read_bytes()).hexdigest()}
        self.open_document(working)
        result={'loaded':True,'archive_path':str(path),'previous_backup':backup,
                'archive_unchanged':path.read_bytes()==raw,**self.status()}
        if self.path_warning():
            result['save_warning']=self.path_warning()
        return result
