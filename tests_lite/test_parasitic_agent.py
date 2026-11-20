# ruff: noqa: F811
from types import SimpleNamespace
from uuid import uuid4

from colayout_lite.agent_tools.session import AgentSession
from colayout_lite.autoroute.service import Candidate
from colayout_lite.autoroute.models import Proposal
from colayout_lite.autoroute.validation import checked_operations
from colayout_lite.drc.models import DrcProfile
from colayout_lite.editor import Editor
from colayout_lite.model import Shape
from colayout_lite.regions.models import OperationRegion
from tests_lite.test_parasitic_rc import profile, rc
from tests_lite.test_parasitic_view import app, host_window, wait_until


class FakeRoutingSession:
    def __init__(self, editor, drc, region_id, folder, *, layers=None):
        self.id = uuid4().hex
        self.folder = folder / self.id
        self.folder.mkdir(parents=True)
        self.region_id = region_id
        self.cancelled = __import__('threading').Event()
        self.selected_layers = layers
        shape = Shape(kind='path', lpp=('wire_a', 'drawing'), points=((0, -20000), (20000, -20000)),
                      width=2000, style='extendExtend', net_intent='A')
        self.candidate = Candidate('candidate', Proposal(title='synthetic', shapes=(shape,)),
            {'status': 'ready'}, ())
        self.problem = SimpleNamespace(pins=[{'net': 'A'}], layers=[SimpleNamespace(name='wire_a')], via_rules=())

    def generate(self, algorithms, progress):
        return [self.candidate]

    def apply(self, editor, candidate_id, drc):
        operations = checked_operations(self.candidate.proposal, self.problem, editor.profile)
        staging = Editor(editor.w, editor.profile, scope_id=self.region_id)
        staging.apply(operations, expected_revision=editor.w.revision)
        editor.publish(staging.w)
        return {'no_op': False, 'object_ids': [op.uid for op in operations]}


def test_agent_commit_and_frozen_route_events(app, profile, monkeypatch, tmp_path):
    host = host_window(profile, monkeypatch)
    editor = host.editor
    region = 'region'
    model = OperationRegion(id=region, name=region, bbox=((-50000, -50000), (50000, 50000)),
                            object_uids=('wire', 'bb'))
    editor.w = editor.w.model_copy(update={'local_data': editor.w.local_data.model_copy(
        update={'regions': {region: model}})})
    editor.scope_id = region
    drc = DrcProfile(id='rules', technology_fingerprint=profile.native_fingerprint)
    session = AgentSession(editor, drc, tmp_path / 'agent', autoroute_enabled=lambda: True)
    commits = []
    routes = []
    session.subscribe_commits = editor.subscribe_commits(commits.append)
    unsubscribe_routes = session.tasks.subscribe_routes(routes.append)
    monkeypatch.setattr('colayout_lite.agent_tools.region_tasks.RoutingSession', FakeRoutingSession)
    try:
        def call(tool, args):
            return session.invoke(tool, args, client_id='layout')
        token = call('claim_region', {'region_id': region, 'agent_name': 'layout'})['result']['session_token']
        before = editor.w
        edited = call('edit_region', {'session_token': token, 'mode': 'apply', 'expected_revision': editor.w.revision,
            'operations': [{'action': 'create_path90', 'params': {'lpp': ['wire_a', 'drawing'],
                'points': [[0, -15], [10, -15]], 'width': 1, 'net_intent': 'A'}}]})
        assert edited['ok']
        assert commits[-1].before is before and commits[-1].after is editor.w
        generated = call('route_region', {'session_token': token, 'action': 'generate', 'expected_revision': editor.w.revision})
        job_id = generated['result']['job_id']
        job = session.jobs[job_id]
        job.future.result(timeout=3)
        session.tasks.poll(job)
        assert routes[-1].action == 'candidate'
        event = routes[-1]
        assert event.baseline is editor.w and event.candidate is not event.baseline
        assert event.scope_id == region and event.target_uids
        host.parasitics.agent_route_event(event)
        assert wait_until(app, lambda: host.parasitics.estimate is not None)
        applied = call('route_region', {'session_token': token, 'action': 'apply', 'job_id': job_id,
            'candidate_id': 'candidate', 'expected_revision': editor.w.revision})
        assert applied['ok']
        assert any(row.action == 'discard' for row in routes)
        tools = call('tools', {})
        assert 'parasitic' not in str(tools).lower() and 'rc_model_digest' not in str(tools)
    finally:
        unsubscribe_routes()
        session.close()
        host.close()
