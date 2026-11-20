"""Synthetic facts through the actual perception, agent, briefing and budget callers."""
import json
from types import SimpleNamespace

import pytest
from test_objective_completion_continue import harness as harness
from test_objective_completion_continue import objective
from test_story_source import artifact, load, policy_hash, snapshot

from addons.system_one.hybrid import HybridDecider
from addons.system_one.questions import plan_decision, reduce_state
from pokegent.history import context_diff
from pokegent.options.models import ChoiceResult, to_system_one_payload
from pokegent.server import ExternalDecider
from pokegent.state.briefing import trim_briefing


def test_facts_unknown_consumed_proof_and_discharged_history(tmp_path):
    data = artifact()
    data['nodes'][0]['objective']['success_probes'] = [dict(
        kind='any_of', children=[dict(kind='item', item=1, min_quantity=1),
                                  dict(kind='flag', flag=1)])]
    source = load(tmp_path, data)
    facts = source.facts(snapshot())
    assert facts['status'] == 'available'
    assert facts['next_challenge'] == {'label': 'Resolve q', 'state': 'unmet'}
    assert facts['prerequisites'] == [{'label': 'Resolve p', 'state': 'unmet'}]
    assert source.facts(snapshot(1))['counts']['proven'] == 1
    missing = snapshot()
    missing.flags = None
    assert source.facts(missing)['counts']['unknown'] == 1
    assert source.facts(missing)['status'] == 'unknown'
    assert source.facts(snapshot(usable=False))['status'] == 'unknown'
    assert source.facts(snapshot(2))['status'] == 'complete'
    # A completed intermediate discharges its consumed prerequisite too.
    data['nodes'].append(dict(id='r', requires=['q'], objective=dict(
        objective_id='story_r', revision=1, kind='hold', rationale='Resolve r',
        success_probes=[dict(kind='flag', flag=3)])))
    data['policy'] = dict(order=['p', 'q', 'r'], required=['r'])
    data['policy_sha256'] = policy_hash(data['policy'])
    facts = load(tmp_path, data).facts(snapshot(2))
    assert facts['prerequisites'] == [{'label': 'Resolve q', 'state': 'proven'}]


def test_summary_unicode_caps_and_whole_unit_budget(tmp_path):
    data = artifact()
    template = data['nodes'][0]['objective']
    data['nodes'] = [dict(id=str(n), requires=[] if n < 8 else [str(i) for i in range(8)],
                          objective={**template, 'objective_id': f'node_{n}',
                                     'rationale': '\U0001f600' * 500}) for n in range(9)]
    data['policy'] = dict(order=[str(n) for n in range(9)], required=['8'])
    data['policy_sha256'] = policy_hash(data['policy'])
    facts = load(tmp_path, data).facts(snapshot())
    assert facts['counts'] == dict(proven=0, unmet=8, unknown=0, total=8)
    assert facts['truncated'] and len(facts['prerequisites']) <= 6
    assert len(json.dumps(facts).encode()) <= 1536
    ordinary = {**facts, 'prerequisites': [{'label': str(i), 'state': 'unmet'}
                                         for i in range(6)]}
    for budget in range(20, 800, 20):
        reduced = reduce_state({'scene': 'field.free', 'story_facts': ordinary,
                                'other': list(range(20))}, budget)
        assert 'story_facts' not in reduced.state or reduced.state['story_facts'] == ordinary
        assert all('story_facts' not in path for step in reduced.steps
                   for path in step.get('paths', []))
    assert 'story_facts' not in reduce_state({'story_facts': ordinary}, 1).state
    briefing = trim_briefing({'story_facts': ordinary}, max_bytes=200)
    assert 'story_facts' not in briefing and 'story_facts' in briefing['diagnostics']['trimmed']
    assert context_diff({'story_facts': facts}, {'story_facts': ordinary}) == 'nothing else changed'


@pytest.mark.parametrize('fallback', [None, 'dex'])
def test_default_and_dex_do_not_attach_facts(harness, fallback):
    from pokegent.goals.dex_fallback import DexFallback
    h = harness(amount=99)
    if fallback:
        h.session.fallback = DexFallback(None)
    h.session.refresh()
    assert 'story_facts' not in to_system_one_payload(h.runner._perceive().frame)['state']


def test_attachment_omits_complete_unit_on_leak_and_never_mutates_frame(harness, tmp_path, caplog):
    h = harness(amount=99)
    h.session.refresh()
    before = h.runner._perceive()
    data = artifact()
    data['nodes'][0]['objective']['rationale'] = 'Forbidden pointer 0x08001234'
    h.session.fallback = load(tmp_path, data)
    bad = h.runner._perceive()
    assert 'story_facts' not in to_system_one_payload(bad.frame)['state']
    assert 'story facts omitted' in caplog.text
    assert bad.frame.state_fingerprint == before.frame.state_fingerprint
    assert bad.frame.options == before.frame.options
    assert 'story_facts' not in before.frame.context


def test_actual_perceiver_recomputes_from_supplied_snapshot_without_reading(harness, tmp_path,
                                                                          monkeypatch):
    from pokegent.games.gen3.firered.reader.schemas import FlagSet
    h = harness(amount=99)
    h.session.refresh()
    original = h.runner._perceive()
    h.session.fallback = load(tmp_path)
    current = original.snapshot
    monkeypatch.setattr(h.source, 'read_snapshot', lambda: pytest.fail('second reader'))
    frames = []
    for flags in (FlagSet(), FlagSet(set_ids=(1,)), None):
        supplied = current.model_copy(update={'flags': flags})
        perceived = h.perceiver._perceive('same', None, (), confirm=False, snapshot=supplied)
        assert perceived.snapshot is supplied
        frames.append(perceived.frame)
        assert perceived.frame.options == original.frame.options
        assert perceived.frame.state_fingerprint == original.frame.state_fingerprint
    facts = [to_system_one_payload(frame)['state']['story_facts'] for frame in frames]
    assert [f['prerequisites'][0]['state'] for f in facts] == ['unmet', 'proven', 'unknown']
    assert facts[-1]['status'] == 'unknown'
    assert 'story_facts' not in original.frame.context and current.flags == FlagSet()


@pytest.mark.parametrize('mode', ['agent', 'hybrid'])
@pytest.mark.parametrize('refresh', [False, True])
def test_actual_agent_hybrid_manual_goal_snapshot_briefing_and_frame_pairing(
        harness, tmp_path, monkeypatch, mode, refresh):
    monkeypatch.setattr('pokegent.state.briefing.map_names.map_name', lambda *args: 'Test Meadow')
    monkeypatch.setattr('pokegent.server.ThreadingHTTPServer', lambda *args: SimpleNamespace(
        server_address=('127.0.0.1', 19760), serve_forever=lambda: None))
    h = harness(set_by='operator', amount=99)
    source = load(tmp_path)
    h.session.fallback = source
    observed = []
    facts_method = source.facts

    def facts_spy(snap):
        observed.append(snap)
        return facts_method(snap)

    monkeypatch.setattr(source, 'facts', facts_spy)
    monkeypatch.setattr(source, 'from_snapshot', lambda s: pytest.fail('manual fallback admission'))
    assert h.runner._iteration() is None  # real history decoration on next decision
    original_goal = h.store.path.read_bytes()
    original_history = h.store.history_path.read_bytes()
    external = ExternalDecider(port=19760)
    plans = []

    def system1(frame):
        plans.append(plan_decision(frame))
        return ChoiceResult(option_id='tap:b', confidence=0)

    if mode == 'hybrid':
        decider = HybridDecider(SimpleNamespace(decide=system1), port=19760, external_timeout=1)
        decider.agent = decider.external = external
    else:
        decider = external
    h.runner.decider = decider
    decider.bind_pending_frame(h.runner._refresh_pending_frame)
    requests, errors, built, executed, briefings = [], [], [], [], []
    perceive, execute = h.perceiver._perceive, h.executor.execute

    def perceive_spy(*args, **kwargs):
        result = perceive(*args, **kwargs)
        if result.frame:
            built.append(result)
        return result

    def execute_spy(frame, option_id):
        executed.append(frame)
        assert 'recent_choices' not in frame.context
        return execute(frame, option_id)

    def requested(frame_id, offered):
        requests.append((frame_id, offered))
        try:
            state = external._frame['state']
            facts = to_system_one_payload(offered)['state']['story_facts']
            assert state['story_facts'] == facts
            briefings.append(json.loads(json.dumps(external._briefing)))
            assert briefings[-1]['story_facts'] == facts
            if len(requests) == 1:
                assert observed[-1] is built[0].snapshot
                assert h.store.path.read_bytes() == original_goal
                assert h.store.history_path.read_bytes() == original_history
                assert 'recent_choices' in offered.context
                if refresh:
                    assert h.store.propose(objective(
                        2, objective_id='second', amount=99, set_by='operator')).accepted
                    return
            else:
                assert briefings[-1] == briefings[0]
                assert facts == to_system_one_payload(requests[0][1])['state']['story_facts']
                with pytest.raises(LookupError, match='stale frame'):
                    external.choose(dict(frame_id=requests[0][0], option_id='tap:b'))
            assert external.choose(dict(frame_id=frame_id, option_id='tap:b'))['accepted']
        except BaseException as exc:
            errors.append(exc)
            external._stop_requested.set()

    monkeypatch.setattr(h.perceiver, '_perceive', perceive_spy)
    monkeypatch.setattr(h.executor, 'execute', execute_spy)
    external.bind_agent_request(requested)
    status = h.runner._iteration()
    assert not errors, errors
    assert status is None
    assert len(requests) == (2 if refresh else 1) and len(executed) == 1
    assert any(executed[0] is p.frame for p in built)
    assert executed[0].goal_id == ('second' if refresh else 'first')
    if refresh:
        assert built[0].snapshot is built[1].snapshot
        assert executed[0] is requests[-1][1]
    else:
        assert executed[0] is built[0].frame and executed[0] is not requests[0][1]
        assert h.store.path.read_bytes() == original_goal
        assert h.store.history_path.read_bytes() == original_history
    if mode == 'hybrid':
        assert plans[0].state['story_facts'] == briefings[0]['story_facts']
    assert h.state.actions == 2
