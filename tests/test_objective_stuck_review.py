"""Synthetic stuck episodes and explicit review without objective takeover."""

from types import SimpleNamespace

import pytest
from test_objective_completion_continue import harness as harness

from pokegent.events import EventType
from pokegent.loop import EscalationAction, EscalationResponse


def training(revision=2, *, subscribed=True, objective_id='training'):
    data = dict(objective_id=objective_id, revision=revision, kind='train', set_by='operator',
                success_probes=[dict(kind='party_level', min_level=99)], forbid=['sell'])
    if subscribed:
        data['next_review_on'] = ['stuck']
    return data


@pytest.mark.parametrize('subscribed', [False, True])
def test_actual_runner_idle_threshold_reviews_once_without_takeover(harness, subscribed):
    h = harness(amount=99)
    assert h.store.propose(training(subscribed=subscribed)).accepted
    h.session.tracker.stuck_after = 2
    requests = []
    h.runner.escalation = SimpleNamespace(escalate=lambda request:
        requests.append(request) or EscalationResponse(EscalationAction.CONTINUE))
    retained = h.store.path.read_bytes()
    for _ in range(6):
        assert h.runner._iteration() is None
    stuck = [event for event in h.events.of_type(EventType.PROGRESS_STUCK)
             if event.source == 'objective']
    assert len(stuck) == 1 and stuck[0].details['idle_updates'] == 2
    reviews = [request for request in requests if request.kind.value == 'objective_review']
    assert len(reviews) == int(subscribed)
    if subscribed:
        assert reviews[0].details['objective_id'] == 'training'
        assert reviews[0].details['revision'] == 2
        assert h.session.review_pending
    assert h.store.path.read_bytes() == retained and h.store.revision() == 2
    assert h.store.current().forbid == ('sell',)
    assert not h.session.done and h.state.actions == 6


# Captured from the pre-field store at merged main51345e5; entirely synthetic.
PRE_FIELD_CURRENT = (
    '{\n  "expires_at": null,\n  "forbid": [\n    "sell"\n  ],\n  "kin'
    'd": "train",\n  "objective_id": "old_operator",\n  "rationale"'
    ': "",\n  "revision": 1,\n  "schema_version": "objective/1",\n  '
    '"set_by": "operator",\n  "success_probes": [\n    {\n      "kin'
    'd": "party_level",\n      "min_level": 99\n    }\n  ],\n  "targe'
    't": {\n    "count": null,\n    "item": null,\n    "map": null,\n'
    '    "npc": null,\n    "quantity": null,\n    "species": null,\n'
    '    "warp": null,\n    "x": null,\n    "y": null\n  }\n}\n'
)
PRE_FIELD_HISTORY = (
    '{"at": "2026-11-19T20:48:35.292444+00:00", "event": "accepte'
    'd", "objective": {"expires_at": null, "forbid": ["sell"], "k'
    'ind": "train", "objective_id": "old_operator", "rationale": '
    '"", "revision": 1, "schema_version": "objective/1", "set_by"'
    ': "operator", "success_probes": [{"kind": "party_level", "mi'
    'n_level": 99}], "target": {"count": null, "item": null, "map'
    '": null, "npc": null, "quantity": null, "species": null, "wa'
    'rp": null, "x": null, "y": null}}, "objective_id": "old_oper'
    'ator", "reasons": [], "revision": 1, "seq": 1}\n'
)


def test_prefield_current_history_survive_cold_restart(tmp_path):
    from pokegent.goals.store import ObjectiveStore
    path = tmp_path / 'objective.json'
    path.write_text(PRE_FIELD_CURRENT)
    path.with_name(path.name + '.history.jsonl').write_text(PRE_FIELD_HISTORY)
    restarted = ObjectiveStore(path)
    active = restarted.current()
    assert active is not None and active.objective_id == 'old_operator'
    assert active.set_by == 'operator' and active.forbid == ('sell',)
    assert restarted.revision() == 1
    assert not restarted.propose(training(2, subscribed=False), expected_revision=0).accepted
    assert path.read_text() == PRE_FIELD_CURRENT


@pytest.mark.parametrize('policy,expected', [('continue', None), ('pause', 'escalated'),
                                           ('stop', 'stopped')])
def test_actual_runner_review_policy(harness, policy, expected):
    from pokegent.app import RecordingSink
    h = harness(amount=99)
    assert h.store.propose(training()).accepted
    h.session.tracker.stuck_after = 2
    h.runner.escalation = RecordingSink(review_action=policy)
    assert h.runner._iteration() is None
    assert h.runner._iteration() is None
    status = h.runner._iteration()
    assert (status.value if status is not None else None) == expected
    assert h.runner.counters.objective_reviews == 1
    assert h.store.current().objective_id == 'training' and h.store.revision() == 2
    assert h.session.snapshot()['review_pending'] and not h.session.completed
    assert h.state.actions == (3 if policy == 'continue' else 2)


def test_stale_review_does_not_stop_concurrent_operator_successor(harness):
    from pokegent.loop import EscalationAction, EscalationResponse
    h = harness(amount=99)
    assert h.store.propose(training()).accepted
    h.session.tracker.stuck_after = 1
    requests = []

    def review(request):
        requests.append(request)
        assert h.store.propose(training(3, objective_id='new_operator')).accepted
        return EscalationResponse(EscalationAction.STOP)

    h.runner.escalation = SimpleNamespace(escalate=review)
    assert h.runner._iteration() is None
    assert h.runner._iteration() is None
    assert h.state.actions == 1  # re-perceive the accepted new revision before input
    assert requests[0].details['objective_id'] == 'training'
    assert requests[0].details['revision'] == 2
    assert h.runner._iteration() is None
    assert not h.session.review_pending and h.session.objective.revision == 3
    assert h.state.actions == 2 and not h.runner.stop_event.is_set()


def test_queued_identity_survives_switch_without_marking_new_goal_pending(harness):
    h = harness(amount=99)
    assert h.store.propose(training()).accepted
    h.session.refresh()
    h.session.tracker.note_stuck({'source': 'synthetic'})
    assert h.store.propose(training(3, objective_id='next_operator')).accepted
    h.session.refresh()
    requests = h.session.take_reviews()
    assert [(r.objective_id, r.revision) for r in requests] == [('training', 2)]
    assert not h.session.review_pending and not h.session.take_reviews()
    assert h.session.snapshot()['reviews_requested'] == 1


def test_opt_in_field_is_serialized_but_default_preserves_old_shape(tmp_path):
    from pokegent.goals.models import Objective
    from pokegent.goals.store import ObjectiveStore
    assert 'next_review_on' not in Objective.model_validate(training(subscribed=False)).model_dump()
    store = ObjectiveStore(tmp_path / 'goal.json')
    assert store.propose(training(1)).accepted
    assert '"next_review_on": [' in store.path.read_text()
    restarted = ObjectiveStore(store.path)
    assert restarted.current().next_review_on == ('stuck',)
    assert restarted.current().forbid == ('sell',) and restarted.revision() == 1


@pytest.mark.parametrize('value', [['blocked'], ['stuck', 'stuck'], 'stuck', True, None])
def test_unsupported_review_subscription_rejected(value):
    from pokegent.goals.models import Objective
    with pytest.raises(ValueError):
        Objective.model_validate({**training(), 'next_review_on': value})


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('policy', ['continue', 'pause', 'stop'])
def test_config_threshold_and_policy_reach_owned_session(tmp_path, modern, policy):
    from pokegent.app import config_from_mapping, load_config_file, make_objective_session
    path = tmp_path / 'config.yaml'
    path.write_text(('decider: first\n' if modern else '') +
                    'run:\n  objective_stuck_after: 2\n  on_objective_review: '+policy+'\n')
    raw = load_config_file(path)
    cfg = config_from_mapping({k: v for k, v in raw.items() if k != 'hybrid'})
    session = make_objective_session(tmp_path / 'objective.json',
                                     stuck_after=cfg.objective_stuck_after)
    assert session.tracker.stuck_after == 2 and cfg.on_objective_review == policy


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '2', None])
def test_invalid_stuck_threshold_rejected(value):
    from pokegent.app import AppError, config_from_mapping
    from pokegent.goals.tracker import ObjectiveTracker
    with pytest.raises(ValueError, match='objective_stuck_after'):
        ObjectiveTracker(stuck_after=value)
    with pytest.raises(AppError, match='objective_stuck_after'):
        config_from_mapping({'run': {'objective_stuck_after': value}})


def test_supplied_tracker_and_session_keep_their_threshold(tmp_path):
    from pokegent.app import make_objective_session
    from pokegent.goals.source import ObjectiveSession
    from pokegent.goals.store import ObjectiveStore
    from pokegent.goals.tracker import ObjectiveTracker
    tracker = ObjectiveTracker(stuck_after=7)
    store = ObjectiveStore(tmp_path / 'objective.json')
    session = ObjectiveSession(store, tracker=tracker, stuck_after=2)
    assert session.tracker is tracker and tracker.stuck_after == 7
    assert make_objective_session(session, stuck_after=3) is session
    assert tracker.stuck_after == 7


def changed(snapshot, **updates):
    from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
    values = {k: getattr(snapshot, k) for k in type(snapshot).model_fields
              if k not in {'schema_version', 'state_hash'}}
    values.update(updates)
    return StateSnapshot.create(**values)


def resolved(data):
    from pokegent.goals.validate import validate
    verdict = validate(data)
    assert verdict.ok, verdict.reasons
    return verdict.resolved


@pytest.mark.parametrize('progress', ['cell', 'flags', 'probe'])
def test_progress_rearms_single_episode_and_invalid_frames_do_not_count(harness, progress):
    from pokegent.games.gen3.firered.reader.schemas import FlagSet
    from pokegent.goals.tracker import ObjectiveTracker
    h = harness(amount=99)
    tracker = ObjectiveTracker(stuck_after=2)
    data = training()
    data['success_probes'].append(dict(kind='money_at_least', amount=1))
    tracker.set_objective(resolved(data))
    snap = h.source.read_snapshot()
    tracker.update(snap)
    h.state.valid = False
    invalid = h.source.read_snapshot()
    h.state.valid = True
    for _ in range(5):
        assert not tracker.update(invalid)
    assert not tracker.reviews
    assert not tracker.update(snap)
    assert tracker.update(snap)[0].type == 'progress.stuck'
    for _ in range(5):
        assert not tracker.update(snap)
    assert len(tracker.reviews) == 1
    if progress == 'cell':
        snap = changed(snap, player=snap.player.model_copy(update={'x': 4}))
    elif progress == 'flags':
        snap = changed(snap, flags=FlagSet(set_ids=(1,)))
    else:
        snap = changed(snap, player=snap.player.model_copy(update={'money': 1}))
    tracker.update(snap)
    tracker.update(snap)
    assert tracker.update(snap)[0].type == 'progress.stuck'
    assert len(tracker.reviews) == 2 and not tracker.completed


def test_same_revision_different_id_resets_and_queue_remains_bounded(harness):
    from pokegent.goals.tracker import ObjectiveTracker
    h = harness(amount=99)
    tracker = ObjectiveTracker(stuck_after=1)
    snap = h.source.read_snapshot()
    for n in range(80):
        events = tracker.set_objective(resolved(training(1, objective_id=f'goal_{n}')))
        assert events[0].details['objective_id'] == f'goal_{n}'
        tracker.update(snap)
        assert not tracker.reviews or tracker.reviews[-1].objective_id != f'goal_{n}'
        tracker.update(snap)
    assert len(tracker.reviews) == 64
    assert tracker.reviews[0].objective_id == 'goal_16'
    assert tracker.reviews[-1].objective_id == 'goal_79'


def test_new_cells_avoid_stall_but_known_two_map_ping_stalls(harness):
    from pokegent.goals.tracker import ObjectiveTracker
    h = harness(amount=99)
    tracker = ObjectiveTracker(stuck_after=2)
    tracker.set_objective(resolved(training()))
    snap = h.source.read_snapshot()
    for n in range(10):
        tracker.update(changed(snap, player=snap.player.model_copy(update={'x': n * 4})))
    assert not tracker.reviews
    for n in [0, 1, 0, 1, 0]:
        tracker.update(changed(snap, player=snap.player.model_copy(update={'map_num': n})))
    assert len(tracker.reviews) == 1


@pytest.mark.parametrize('mode', ['hold', 'completed', 'absent'])
def test_exempt_goals_never_queue_review(harness, mode):
    from pokegent.goals.tracker import ObjectiveTracker
    h = harness(amount=99)
    tracker = ObjectiveTracker(stuck_after=1)
    data = training()
    if mode == 'hold':
        data['kind'] = 'hold'
    elif mode == 'completed':
        data['success_probes'] = [dict(kind='money_at_least', amount=0)]
    tracker.set_objective(None if mode == 'absent' else resolved(data))
    for _ in range(5):
        tracker.update(h.source.read_snapshot())
        tracker.note_stuck({'source': 'no_progress'})
    assert not tracker.reviews


def test_failing_review_sink_pauses_without_touching_operator_goal(harness):
    h = harness(amount=99)
    assert h.store.propose(training()).accepted
    h.session.tracker.stuck_after = 1

    def fail(request):
        raise OSError('synthetic reviewer unavailable')

    h.runner.escalation = SimpleNamespace(escalate=fail)
    assert h.runner._iteration() is None
    assert h.runner._iteration().value == 'escalated'
    assert h.store.current().set_by == 'operator' and h.store.current().forbid == ('sell',)
    assert h.state.actions == 1


def test_pending_review_clears_on_new_revision_and_expiry_never_authorizes_fallback(harness):
    from datetime import datetime, timedelta, timezone

    from pokegent.goals.source import ObjectiveSession
    from pokegent.goals.store import ObjectiveStore
    h = harness(amount=99)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    assert h.store.propose({**training(), 'expires_at': expiry.isoformat()}).accepted
    h.session.refresh()
    h.session.tracker.note_stuck({'source': 'synthetic'})
    assert len(h.session.take_reviews()) == 1 and h.session.review_pending
    assert h.store.propose(training(3, objective_id='newer')).accepted
    h.session.refresh()
    assert not h.session.review_pending
    assert not h.store.propose(training(4), expected_revision=2).accepted
    # A pending review of an expired operator goal must not invent completion or fallback.
    assert h.store.propose({**training(4), 'expires_at': expiry.isoformat()}).accepted
    h.session.refresh()
    h.session.tracker.note_stuck({'source': 'synthetic'})
    h.session.take_reviews()
    h.store.clock = lambda: expiry + timedelta(seconds=1)
    restarted = ObjectiveStore(h.store.path, clock=h.store.clock)
    session = ObjectiveSession(restarted, fallback=SimpleNamespace(
        from_snapshot=lambda _: pytest.fail('expired review cannot authorize fallback')))
    session.refresh()
    session.observe(h.source.read_snapshot(), safe_boundary=True, clear_completed=True)
    assert restarted.current() is None and restarted.expired()
    assert 'expired' in session.fallback_error and not session.review_pending
    assert restarted.revision() == 4


@pytest.mark.parametrize('closed_loop', [False, True])
def test_actual_hybrid_recovery_remains_finite_with_continue_review(
        tmp_path, monkeypatch, closed_loop):
    from test_hybrid_stuck_escalation import harness as hybrid_fixture

    from pokegent.goals.source import ObjectiveSession
    from pokegent.goals.store import ObjectiveStore
    from pokegent.recovery import ProgressMonitor

    factory = hybrid_fixture.__wrapped__(monkeypatch)
    kwargs = dict(continue_sink=True)
    if closed_loop:
        kwargs.update(alternate=True, semantic_at=tuple(range(1, 13)),
                      map_ping_progressed=True, no_progress_limit=3)
    runner, calls, actions, requests, events = factory(**kwargs)
    if closed_loop:
        runner._progress = ProgressMonitor(3, now=runner.clock(), loop_window=4,
                                           loop_min_cycles=2)
    store = ObjectiveStore(tmp_path / 'objective.json')
    assert store.propose(training(1)).accepted
    session = ObjectiveSession(store)
    runner.objectives = session
    session.attach_sink(runner._objective_sink)
    result = runner.run()
    assert result.verdict == 'no_progress' and requests
    assert result.status.value == 'safe_pause' and len(actions) < 30
    reviews = [e for e in events.of_type(EventType.ESCALATION_REQUESTED)
               if e.details.get('kind') == 'objective_review']
    assert len(reviews) == 1 and reviews[0].details['objective_id'] == 'training'
    assert result.review_pending and not session.completed
    assert store.current().set_by == 'operator' and store.current().forbid == ('sell',)
    if closed_loop:
        assert any(e.details.get('reason', '').startswith('option_loop:')
                   for e in events.of_type(EventType.PROGRESS_STUCK))
        # Compare the existing positional-progress recovery behavior without review opt-in.
        control, _, control_actions, _, _ = factory(**kwargs)
        control._progress = ProgressMonitor(3, now=control.clock(), loop_window=4,
                                            loop_min_cycles=2)
        other = ObjectiveStore(tmp_path / 'control.json')
        assert other.propose(training(1, subscribed=False)).accepted
        control.objectives = ObjectiveSession(other)
        control.objectives.attach_sink(control._objective_sink)
        baseline = control.run()
        assert result.status == baseline.status and result.verdict == baseline.verdict
        assert len(actions) == len(control_actions)

    else:
        assert len(calls) == 2 and len(actions) == 4 and len(requests) == 2


@pytest.mark.parametrize('policy', ['hold', '', None, True])
def test_invalid_review_policy_rejected(policy):
    from pokegent.app import AppError, config_from_mapping
    with pytest.raises(AppError, match='on_objective_review'):
        config_from_mapping({'run': {'on_objective_review': policy}})
