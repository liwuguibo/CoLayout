# ruff: noqa: F811
from types import SimpleNamespace

import pytest

from colayout_lite.drc.models import DrcProfile
from colayout_lite.parasitic_models import RcExtractionEvidence
from colayout_lite.process_measurements import policy_key
from colayout_lite.process_package import ProcessPackage, changes
from colayout_lite.process_review import local_settings_review
from tests_lite.test_parasitic_rc import profile, rc


def test_rc_model_digest_is_separate_from_native_policy_and_package_fingerprint(profile, rc):
    package = ProcessPackage(technology=profile)
    assert profile.rc_model_digest == rc.fingerprint()
    without = profile.model_copy(update={'parasitics': None})
    assert without.rc_model_digest is None
    assert policy_key(profile) == policy_key(without)
    changed = rc.model_copy(update={'metals': {**rc.metals, 'wire_a': rc.metals['wire_a'].model_copy(
        update={'sheet_resistance_ohm_per_sq': .11})}})
    after = profile.model_copy(update={'parasitics': changed})
    assert after.rc_model_digest != profile.rc_model_digest
    assert ProcessPackage(technology=after).fingerprint() != package.fingerprint()
    assert 'RC 寄生模型' in changes(package, ProcessPackage(technology=after))


def test_rc_evidence_is_bound_to_the_exact_model(rc):
    evidence = RcExtractionEvidence(rc_model_digest=rc.fingerprint(), input_digest='frozen', extraction_settings={'mode': 'RCC'},
        status='xrc_checked', summary='synthetic evidence')
    assert evidence.current(rc)
    changed = rc.model_copy(update={'corner': 'other'})
    assert not evidence.current(changed)


def test_rc_only_change_reuses_native_qualification_but_has_its_own_review(profile):
    drc = DrcProfile(id='rules', technology_fingerprint=profile.native_fingerprint)
    current = ProcessPackage(technology=profile.model_copy(update={'parasitics': None}), drc=drc)
    proposed = ProcessPackage(technology=profile, drc=drc)
    connection = profile.model_copy(update={'parasitics': None})
    del connection
    entry = {'confirmed': True, 'connection': '/tmp/connection.json', 'sample': {'library': 'L', 'cell': 'C'}}
    service = SimpleNamespace(store=SimpleNamespace(entries=lambda: {profile.key: entry}),
        load=lambda key: current)
    proof = local_settings_review(service, proposed, {'library': 'L', 'cell': 'C'}, '/tmp/connection.json')
    assert proof['kind'] == 'reviewed_local_settings'
    assert proof['changed_fields'] == ['parasitics']
    assert proof['native_roundtrip_on_this_host'] is False


def test_rc_model_rejects_unknown_layers(profile):
    raw = profile.model_dump(mode='json')
    raw['parasitics']['metals']['unknown'] = raw['parasitics']['metals']['wire_a']
    with pytest.raises(ValueError, match='unknown metal'):
        type(profile).model_validate(raw)
