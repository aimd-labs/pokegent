"""Bounded synthetic history through real runner and provider payloads."""

from types import SimpleNamespace

import pytest
from test_objective_completion_continue import harness as harness
from test_objective_completion_continue import objective

from pokegent.app import AppError, config_from_mapping, load_config_file
from pokegent.events import EventType
from pokegent.loop import RunStatus
from pokegent.options.models import ChoiceResult, to_system_one_payload


def test_actual_runner_shows_previous_result_only_to_decider(harness, monkeypatch):
    h = harness(amount=99)
    originals, shown, payloads = [], [], []
    execute = h.executor.execute

    def spy(frame, option_id):
        originals.append(frame)
        assert 'recent_choices' not in frame.context
        return execute(frame, option_id)

    def decide(frame):
        shown.append(frame)
        payloads.append(to_system_one_payload(frame))
        return ChoiceResult(option_id='tap:b')

    monkeypatch.setattr(h.executor, 'execute', spy)
    h.runner.decider = SimpleNamespace(decide=decide)
    assert h.runner.run().status is RunStatus.MAX_STEPS
    assert 'recent_choices' not in payloads[0]['state']
    assert 'done' in payloads[1]['state']['recent_choices'][0]
    assert len(payloads[-1]['state']['recent_choices']) == 3
    assert len(originals) == len(shown) == 6
    for plain, decorated in zip(originals, shown):
        assert plain.state_fingerprint == decorated.state_fingerprint
        assert plain.options is decorated.options
        assert plain.context == {k: v for k, v in decorated.context.items()
                                 if k != 'recent_choices'}
    assert all(result.verified_outcome is True for result in h.results)


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('keep', [0, 3, 8])
def test_config_history_reaches_runner(tmp_path, modern, keep):
    path = tmp_path / 'config.yaml'
    path.write_text(('decider: first\n' if modern else '') +
                    f'run:\n  decision_history: {keep}\n')
    raw = load_config_file(path)
    cfg = config_from_mapping({k: v for k, v in raw.items() if k != 'hybrid'})
    assert cfg.runner_config().decision_history == keep


@pytest.mark.parametrize('keep', [True, False, -1, 9, 1.5, '3', None])
def test_invalid_config_history_is_rejected(keep):
    with pytest.raises((AppError, ValueError), match='decision_history'):
        config_from_mapping({'run': {'decision_history': keep}}).runner_config()


@pytest.fixture
def frame(harness):
    h = harness(amount=99)
    h.session.refresh()
    return h.runner._perceive().frame


def context(frame, **values):
    from pokegent.frozen import freeze
    return frame.model_copy(update={'context': freeze(values)})


@pytest.mark.parametrize('keep', [0, 3, 8])
def test_history_retention_order_immutability_and_budget(frame, keep):
    from addons.system_one.questions import reduce_state
    from pokegent.actions._exec.models import ActionResult, ActionStatus
    from pokegent.history import DecisionHistory
    history = DecisionHistory(keep)
    result = ActionResult(status=ActionStatus.APPLIED)
    for n in range(10):
        current = context(frame, map='Test', money=n)
        history.prepare(current)
        history.record(current, 'tap:b', result)
    current = context(frame, map='Test', money=10)
    shown = history.prepare(current)
    if keep == 0:
        assert shown is current
    else:
        lines = to_system_one_payload(shown)['state']['recent_choices']
        assert len(lines) == keep
        assert f'money: {10-keep} -> {11-keep}' in lines[0]
        assert 'money: 9 -> 10' in lines[-1]
        assert 'recent_choices' not in current.context
        with pytest.raises(TypeError):
            shown.context['recent_choices'] = ()
        reduced = reduce_state({'scene': 'field.free', 'recent_choices': lines}, 110)
        remaining = reduced.state.get('recent_choices', [])
        assert not remaining or remaining == lines[-len(remaining):]
        if keep == 8:
            assert reduced.truncated


@pytest.mark.parametrize('status,word', [('applied', 'done'), ('rejected', 'rejected'),
                                        ('interrupted', 'interrupted'),
                                        ('timed_out', 'failed'), ('uncertain', 'failed')])
def test_neutral_outcomes_and_rejected_actions_have_no_diff(frame, status, word):
    from pokegent.actions._exec.models import ActionResult, ActionStatus
    from pokegent.errors import ErrorCode
    from pokegent.history import DecisionHistory
    history = DecisionHistory()
    result = ActionResult(status=ActionStatus(status),
                          error_code=None if status == 'applied' else ErrorCode.E_EMULATOR)
    history.record(context(frame, money=1), 'tap:b', result)
    text = history.prepare(context(frame, money=2)).context['recent_choices'][0]
    assert f': {word}' in text
    assert ('money: 1 -> 2' in text) is (status != 'rejected')
    if status != 'applied':
        assert 'E_EMULATOR' in text


@pytest.mark.parametrize('reset', ['scene', 'goal_id', 'revision', 'turns'])
def test_history_resets_even_after_effect_diff_resolved(frame, reset):
    from pokegent.actions._exec.models import ActionResult, ActionStatus
    from pokegent.history import DecisionHistory
    history = DecisionHistory()
    first = context(frame, turns_played=3)
    history.record(first, 'tap:b', ActionResult(status=ActionStatus.APPLIED), ('a', 1))
    assert 'recent_choices' in history.prepare(first, ('a', 1)).context
    other = context(frame, turns_played=2 if reset == 'turns' else 3)
    if reset == 'scene':
        other = other.model_copy(update={'scene_id': 'battle.menu'})
    key = ('b', 1) if reset == 'goal_id' else ('a', 2) if reset == 'revision' else ('a', 1)
    history.observe(other, key)  # also the reset hook for automatic waits
    assert history.prepare(other, key) is other


def test_map_crossing_keeps_last_choice_and_builder_key_wins(frame):
    from pokegent.actions._exec.models import ActionResult, ActionStatus
    from pokegent.history import DecisionHistory
    history = DecisionHistory()
    result = ActionResult(status=ActionStatus.APPLIED)
    first = context(frame, map='TestA')
    history.record(first, 'tap:b', result)
    history.prepare(first)
    history.record(first, 'tap:b', result)
    moved = context(frame, map='TestB')
    lines = history.prepare(moved).context['recent_choices']
    assert len(lines) == 1 and 'map: TestA -> TestB' in lines[0]
    builder = context(frame, map='TestB', recent_choices=['builder-owned'])
    assert history.prepare(builder) is builder


def test_context_diff_is_bounded_neutral_and_counter_aware():
    from pokegent.history import context_diff
    text = context_diff({'x': 'a' * 500}, {'x': 'b' * 500, 'a': 1, 'b': 2, 'c': 3})
    assert len(text) < 190 and '+1 more changed' in text
    assert context_diff({'turns_played': 1}, {'turns_played': 2}) == (
        'nothing else changed (turns_played 1 -> 2)')
    assert context_diff({}, {}) == 'nothing else changed'


@pytest.mark.parametrize('mode', ['agent', 'hybrid'])
@pytest.mark.parametrize('refresh', [False, True])
def test_actual_pending_choice_executes_paired_plain_frame(harness, monkeypatch, mode, refresh):
    from addons.system_one.hybrid import HybridDecider
    from pokegent.server import ExternalDecider
    monkeypatch.setattr('pokegent.server.ThreadingHTTPServer', lambda *args: SimpleNamespace(
        server_address=('127.0.0.1', 19760), serve_forever=lambda: None))
    h = harness(amount=99)
    assert h.runner._iteration() is None  # seed a real result in history
    external = ExternalDecider(port=19760)
    if mode == 'hybrid':
        decider = HybridDecider(SimpleNamespace(decide=lambda f: ChoiceResult(
            option_id='tap:b', confidence=0)), port=19760, external_timeout=1)
        decider.agent = decider.external = external
    else:
        decider = external
    h.runner.decider = decider
    decider.bind_pending_frame(h.runner._refresh_pending_frame)
    requests, errors, rebuilt, executed = [], [], [], []
    perceive, execute = h.perceiver._perceive, h.executor.execute

    def perceive_spy(*args, **kwargs):
        result = perceive(*args, **kwargs)
        rebuilt.append(result.frame)
        return result

    def execute_spy(frame, option_id):
        executed.append(frame)
        assert 'recent_choices' not in frame.context
        return execute(frame, option_id)

    def requested(frame_id, offered):
        requests.append((frame_id, offered))
        try:
            if len(requests) == 1:
                assert 'recent_choices' in to_system_one_payload(offered)['state']
                if refresh:
                    assert h.store.propose(objective(2, objective_id='second', amount=99)).accepted
                    return
            else:
                assert 'recent_choices' not in offered.context
                with pytest.raises(LookupError, match='stale frame'):
                    external.choose(dict(frame_id=requests[0][0], option_id='tap:b'))
            assert external.choose(dict(frame_id=frame_id, option_id='tap:b'))['accepted']
        except BaseException as exc:
            errors.append(exc)
            external._stop_requested.set()

    monkeypatch.setattr(h.perceiver, '_perceive', perceive_spy)
    monkeypatch.setattr(h.executor, 'execute', execute_spy)
    external.bind_agent_request(requested)
    assert h.runner._iteration() is None
    assert not errors and len(requests) == (2 if refresh else 1)
    assert len(executed) == 1 and any(executed[0] is original for original in rebuilt)
    assert executed[0].goal_id == ('second' if refresh else 'first')
    if refresh:
        assert executed[0].snapshot_seq == requests[0][1].snapshot_seq
        assert executed[0] is requests[-1][1]
    else:
        assert executed[0] is not requests[0][1]
    assert h.state.actions == 2 and not h.runner.stop_event.is_set()


@pytest.mark.parametrize('original_leak', [False, True])
def test_original_guard_precedes_optional_history_degradation(harness, original_leak):
    from pokegent.actions._exec.models import ActionResult, ActionStatus
    h = harness(amount=99)
    h.session.refresh()
    plain = h.runner._perceive().frame
    h.runner.history.record(context(plain, money='0x02000000'), 'tap:b',
                            ActionResult(status=ActionStatus.APPLIED), h.runner._goal_key(plain))
    frame = context(plain, money='0x02000000' if original_leak else 1)
    answer = h.runner._decide(frame)
    if original_leak:
        assert isinstance(answer, RunStatus)
        assert not h.seen
        assert not any(e.details.get('component') == 'decision_history'
                       for e in h.events.of_type(EventType.RUNTIME_DEGRADED))
    else:
        assert answer[0] is frame
        assert h.seen[-1][1] is frame and 'recent_choices' not in frame.context
        assert any(e.details.get('component') == 'decision_history'
                   for e in h.events.of_type(EventType.RUNTIME_DEGRADED))
    assert h.state.actions == 0


def test_runner_cli_setting_and_duplicate_forwarding(monkeypatch, tmp_path):
    from pokegent import cli
    from pokegent.loop import RunnerConfig
    called = []
    monkeypatch.setattr('pokegent.app.run_offline', lambda **kw:
                        called.append(kw) or SimpleNamespace(exit_code=0))
    monkeypatch.setenv('POKEGENT_FIELD_MENU', '1')
    with pytest.raises(SystemExit) as exit:
        cli.main(['run', '--new-game', '--config', str(tmp_path / 'absent.yaml'),
                  '--decision-history', '0'])
    assert exit.value.code == 0 and called[0]['decision_history'] == 0
    assert RunnerConfig().decision_history == 3
    for raw in ({'runner': {'decision_history': 2}},
                {'run': {'decision_history': 1}, 'runner': {'decision_history': 2}}):
        with pytest.raises(AppError, match='decision_history'):
            config_from_mapping(raw)


def test_real_sync_provider_receives_history_state(harness):
    from addons.system_one.mock import MockProvider
    from addons.system_one.provider import SyncDecider
    provider = MockProvider(strategy=lambda state, question: 'tap:b')
    decider = SyncDecider(provider)
    h = harness(amount=99)
    h.runner.decider = decider
    try:
        assert h.runner.run().status is RunStatus.MAX_STEPS
        assert len(provider.calls) == 6
        assert 'recent_choices' not in provider.calls[0]['state']
        assert 'done' in provider.calls[1]['state']['recent_choices'][0]
        assert len(provider.calls[-1]['state']['recent_choices']) == 3
    finally:
        decider.close()


def test_pure_wait_is_not_recorded_as_decider_choice(harness, monkeypatch):
    from dataclasses import replace
    h = harness(amount=99)
    build = h.Builder.build

    def wait_only(self, ctx):
        draft = build(self, ctx)
        return replace(draft, options=tuple(o for o in draft.options if o.id == 'wait'))

    with monkeypatch.context() as patch:
        patch.setattr(h.Builder, 'build', wait_only)
        assert h.runner._iteration() is None
    assert not h.seen and h.state.actions == 0
    assert h.runner._iteration() is None
    assert 'recent_choices' not in h.seen[0][1].context


def test_retried_decider_adds_only_one_executed_history_entry(harness):
    from pokegent.options.models import DeciderTimeout
    h = harness(amount=99)
    shown = []

    def decide(frame):
        shown.append(frame)
        if len(shown) % 2:
            raise DeciderTimeout('synthetic decision timeout')
        return ChoiceResult(option_id='tap:b')

    h.runner.decider = SimpleNamespace(decide=decide)
    assert h.runner._iteration() is None
    assert h.runner._iteration() is None
    assert len(shown) == 4 and h.state.actions == 2
    assert 'recent_choices' not in shown[0].context
    assert len(shown[2].context['recent_choices']) == 1
    assert shown[2] is shown[3]


def test_run_offline_cli_override_reaches_runner_config(tmp_path, monkeypatch):
    from pokegent import app
    original, observed = app.AppConfig, []

    def config(**kwargs):
        value = original(**kwargs)
        observed.append(value)
        return value

    path = tmp_path / 'config.yaml'
    path.write_text('run:\n  decision_history: 8\n')
    monkeypatch.setattr(app, 'AppConfig', config)
    report = app.run_offline(state=None, out_dir=tmp_path / 'out', config_path=path,
                             decision_history=0, decider='invalid-synthetic', log=lambda *_: None)
    assert report.exit_code != 0
    assert observed[0].runner_config().decision_history == 8
    assert observed[-1].runner_config().decision_history == 0
