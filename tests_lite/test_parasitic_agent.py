# ruff: noqa: F811
from types import SimpleNamespace
from uuid import uuid4

from colayout_lite.agent_tools.session import AgentSession
from colayout_lite.autoroute.models import Proposal
from colayout_lite.autoroute.service import Candidate
from colayout_lite.autoroute.validation import checked_operations
from colayout_lite.drc.models import DrcProfile
from colayout_lite.editor import Editor
from colayout_lite.model import Shape
from colayout_lite.regions.models import OperationRegion
from tests_lite.test_parasitic_rc import profile, rc  # noqa: F401
from tests_lite.test_parasitic_view import app, host_window, wait_until  # noqa: F401


class FakeRoutingSession:
    def __init__(self, editor, drc, region_id, folder, *, layers=None):
        self.id = uuid4().hex
        self.folder = folder / self.id
        self.folder.mkdir(parents=True)
        self.region_id = region_id
        self.cancelled = __import__('threading').Event()
        self.selected_layers = layers
        shape = Shape(
            kind='path',
            lpp=('wire_a', 'drawing'),
            points=((0, -20000), (20000, -20000)),
            width=2000,
            style='extendExtend',
            net_intent='A',
        )
        self.candidate = Candidate('candidate', Proposal(title='synthetic', shapes=(shape,)), {'status': 'ready'}, ())
        self.problem = SimpleNamespace(pins=[{'net': 'A'}], layers=[SimpleNamespace(name='wire_a')], via_rules=())

    def generate(self, algorithms, progress):
        return [self.candidate]

    def apply(self, editor, candidate_id, drc):
        operations = checked_operations(self.candidate.proposal, self.problem, editor.profile)
        staging = Editor(editor.w, editor.profile, scope_id=self.region_id)
        staging.apply(operations, expected_revision=editor.w.revision)
        editor.publish(staging.w)
        return {'no_op': False, 'object_ids': [op.uid for op in operations]}


def test_agent_and_human_share_ondemand_parasitic_inspection(app, profile, monkeypatch, tmp_path):  # noqa: F811
    host = host_window(profile, monkeypatch)
    editor = host.editor
    region = 'region'
    model = OperationRegion(
        id=region,
        name=region,
        bbox=((-50000, -50000), (50000, 50000)),
        object_uids=('wire', 'bb'),
    )
    editor.w = editor.w.model_copy(
        update={'local_data': editor.w.local_data.model_copy(update={'regions': {region: model}})}
    )
    editor.scope_id = region
    drc = DrcProfile(id='rules', technology_fingerprint=profile.native_fingerprint)
    session = AgentSession(editor, drc, tmp_path / 'agent', autoroute_enabled=lambda: True)
    commits = []
    routes = []
    unsub_commits = editor.subscribe_commits(commits.append)
    unsubscribe_routes = session.tasks.subscribe_routes(routes.append)
    monkeypatch.setattr('colayout_lite.agent_tools.region_tasks.RoutingSession', FakeRoutingSession)
    try:
        def call(tool, args):
            return session.invoke(tool, args, client_id='layout')

        token = call('claim_region', {'region_id': region, 'agent_name': 'layout'})['result']['session_token']

        # Edit and candidate generation do zero unsolicited RC work
        edited = call(
            'edit_region',
            {
                'session_token': token,
                'mode': 'apply',
                'expected_revision': editor.w.revision,
                'operations': [
                    {
                        'action': 'create_path90',
                        'params': {
                            'lpp': ['wire_a', 'drawing'],
                            'points': [[0, 0], [10, 0]],
                            'width': 1,
                            'net_intent': 'A',
                        },
                    }
                ],
            },
        )
        assert edited['ok']
        created_uids = edited['result']['created_uids']
        assert host.parasitics.estimate is None

        # Rules metadata advertises configured RC capability without leaking weights
        rules = call('inspect_region', {'session_token': token, 'kind': 'rules'})['result']
        assert rules['parasitics']['configured'] is True
        assert rules['parasitics']['mode'] == 'analytic'
        assert rules['parasitics']['rc_model_digest'] == profile.rc_model_digest

        # Agent queries on-demand RC by UIDs and by net
        agent_by_uids = call(
            'inspect_region',
            {'session_token': token, 'kind': 'parasitics', 'uids': created_uids, 'analysis_radius_um': 10.0},
        )
        assert agent_by_uids['ok'], agent_by_uids
        res = agent_by_uids['result']
        assert res['selection']['basis'] == 'uids'
        assert res['selection']['target_uids'] == created_uids
        assert res['resistance_ohm'] > 0
        assert res['ground_capacitance_fF'] > 0
        assert res['coupling_capacitance_fF'] > 0

        agent_by_net = call(
            'inspect_region',
            {'session_token': token, 'kind': 'parasitics', 'net': 'A'},
        )
        assert agent_by_net['ok'], agent_by_net
        assert agent_by_net['result']['selection']['basis'] == 'net'
        assert set(created_uids) <= set(agent_by_net['result']['selection']['target_uids'])

        # Human UI evaluates the exact same UIDs via shared domain service and gets identical numbers
        assert host.parasitics.evaluate_now(created_uids)
        assert wait_until(app, lambda: host.parasitics.estimate is not None)
        assert host.parasitics.estimate.resistance_ohm == res['resistance_ohm']
        assert host.parasitics.estimate.ground_capacitance_fF == res['ground_capacitance_fF']
        assert host.parasitics.estimate.coupling_capacitance_fF == res['coupling_capacitance_fF']

        # Foreign UID is rejected with OBJECT_OUTSIDE_REGION
        bad_uid = call(
            'inspect_region',
            {'session_token': token, 'kind': 'parasitics', 'uids': ['nonexistent_uid']},
        )
        assert not bad_uid['ok'] and bad_uid['error']['code'] == 'OBJECT_OUTSIDE_REGION'

        # Autoroute candidate generation stays silent on ParasiticView unless explicitly inspected
        generated = call(
            'route_region',
            {'session_token': token, 'action': 'generate', 'expected_revision': editor.w.revision},
        )
        job_id = generated['result']['job_id']
        job = session.jobs[job_id]
        job.future.result(timeout=3)
        session.tasks.poll(job)
        assert routes[-1].action == 'candidate'
        applied = call(
            'route_region',
            {
                'session_token': token,
                'action': 'apply',
                'job_id': job_id,
                'candidate_id': 'candidate',
                'expected_revision': editor.w.revision,
            },
        )
        assert applied['ok']
        assert any(row.action == 'discard' for row in routes)
    finally:
        unsub_commits()
        unsubscribe_routes()
        session.close()
        host.close()


def test_agent_parasitics_requires_configured_model(app, profile, monkeypatch, tmp_path):  # noqa: F811
    no_rc_profile = profile.model_copy(update={'parasitics': None})
    host = host_window(no_rc_profile, monkeypatch)
    editor = host.editor
    region = 'region'
    model = OperationRegion(
        id=region,
        name=region,
        bbox=((-50000, -50000), (50000, 50000)),
        object_uids=('wire', 'bb'),
    )
    editor.w = editor.w.model_copy(
        update={'local_data': editor.w.local_data.model_copy(update={'regions': {region: model}})}
    )
    editor.scope_id = region
    drc = DrcProfile(id='rules', technology_fingerprint=no_rc_profile.native_fingerprint)
    session = AgentSession(editor, drc, tmp_path / 'agent')
    try:
        token = session.invoke('claim_region', {'region_id': region, 'agent_name': 'layout'}, client_id='layout')['result']['session_token']
        resp = session.invoke('inspect_region', {'session_token': token, 'kind': 'parasitics'}, client_id='layout')
        assert not resp['ok'] and resp['error']['code'] == 'PARASITICS_NOT_CONFIGURED'
    finally:
        session.close()
        host.close()
