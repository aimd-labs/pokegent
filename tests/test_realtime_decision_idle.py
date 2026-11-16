"""Synthetic frames/RAM only: scoped idle, stale decisions, streaming, and verified input."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ExecutionKind, Tap
from pokegent.actions._exec.registry import (
    ExecutorRegistry,
    ExecutorSpec,
    VerifierRegistry,
    VerifierSpec,
)
from pokegent.app import AppError, config_from_mapping, load_config_file, open_emulator
from pokegent.dashboard.stream.audio import AudioHub
from pokegent.dashboard.stream.frames import FrameHub
from pokegent.emulator import EmuClock, EmulatorActor
from pokegent.errors import ErrorCode
from pokegent.events import EventType, MemoryEventLog
from pokegent.games.gen3.firered.maps.map_schema import MapObject, MapState, PlayerPlacement
from pokegent.games.gen3.firered.reader.battle_reader import BattleSnapshot
from pokegent.games.gen3.firered.reader.decision_idle import observable_state
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    PlayTime,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.reader.snapshot_sources import (
    CompositeRead,
    CompositeSnapshotSource,
)
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.games.gen3.firered.rom.dialog_text import DialogText
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.loop import Runner, RunnerConfig
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option, StaleDecision
from pokegent.pipeline import Observation, Perception, PerceptionKind
from pokegent.server import ExternalDecider
from pokegent.state.scenes import SceneResult
from pokegent.testing.fake_emulator import FakeGBAEmulator


def composite(*, battle=False):
    section = dict(valid=True, confidence=1)
    ui = UiEvidence.model_validate(dict(
        valid=True, main={**section, "in_battle": battle}, tasks=section, text=section,
        script=section,
        menu={**section, "start_menu": dict(cursor_pos=0, num_items=2, window_id=0,
                                            task_active=True)},
        field=section,
    ))
    # A tiny invented room, with no game-derived map data.
    map_state = MapState.model_validate(dict(
        identity=dict(group=0, number=0, layout_id=1, region_section_id=0, map_type=1,
                      map_type_name="room", weather=0, cave=0, floor_num=0, battle_type=0,
                      music=0, biking_allowed=False, allow_escaping=False, allow_running=True,
                      identity_verified=True),
        layout=dict(width=1, height=1, border_width=1, border_height=1, border=[0],
                    primary_tileset=0, secondary_tileset=0, layout_ptr=0),
        grid=dict(columns=1, rows=1, blocks=[0], attributes=[]),
        objects=[MapObject(local_id=1, graphics_id=0, role="npc", x=0, y=0, elevation=0,
                           movement_type=0, movement_type_name=None, range_x=1, range_y=1,
                           trainer_type=0, trainer_range=0, script_ptr=None, hidden_flag_id=0,
                           hidden_flag_set=False, source="live", blocks_tile=True)],
    ))
    snapshot = StateSnapshot.create(
        rom_profile="synthetic", snapshot_seq=0, health=Health(valid=True),
        player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0, map_num=0,
                      map_layout_id=1, x=0, y=0, money=0, coins=0),
        flags=FlagSet(), vars=VarTable(), ui=ui, map=map_state,
    )
    from pokegent.actions.battle.state import BattleBlock
    block = BattleBlock(active=battle, message="Choose an action" if battle else "")
    snapshot = BattleSnapshot.attach(snapshot, block)
    return CompositeRead(snapshot, 0, block, None)


def change_observable(read, case):
    snapshot, ui = read.snapshot, read.snapshot.ui
    if case == "dialog_page":
        return replace(read, text=DialogText(pages=("An auto-advanced page",),
                                             source="printing", page_index=1))
    if case == "menu_cursor":
        menu = ui.menu.model_copy(update={"start_menu": ui.menu.start_menu.model_copy(
            update={"cursor_pos": 1})})
        snapshot = snapshot.model_copy(update={"ui": ui.model_copy(update={"menu": menu})})
    elif case == "npc_movement":
        obj = snapshot.map.objects[0].model_copy(update={"x": 1})
        snapshot = snapshot.model_copy(update={"map": snapshot.map.model_copy(
            update={"objects": (obj,)})})
    elif case == "map_change":
        identity = snapshot.map.identity.model_copy(update={"number": 1})
        snapshot = snapshot.model_copy(update={"map": snapshot.map.model_copy(
            update={"identity": identity})})
    elif case == "script":
        script = ui.script.model_copy(update={"script_ptr": 0x08000011})
        snapshot = snapshot.model_copy(update={"ui": ui.model_copy(update={"script": script})})
    elif case == "battle_phase":
        snapshot = snapshot.model_copy(update={"battle": read.battle.model_copy(
            update={"input_battlers": (0,), "main_func": 0x08000011})})
    else:
        raise AssertionError(case)
    return replace(read, snapshot=snapshot)


def decision(n=0, *, scene="menu.start"):
    return DecisionFrame(
        decision_id=f"choice-{n}", run_id="synthetic", snapshot_seq=n,
        scene_id=scene, scene_confidence=1, state_fingerprint="sha256:" + "0" * 64,
        options=(Option(id="confirm", label="Confirm", legality=Legality.LEGAL,
                        executor_ref="test.confirm", postcondition_ref="test.pressed"),),
    )


class StreamingFake(FakeGBAEmulator):
    def __init__(self):
        super().__init__()
        self.sink = None
        self.thread_ids = set()
        self.key_frames = []

    def set_audio_sink(self, sink):
        self.sink = sink

    def _one_frame(self):
        self.thread_ids.add(threading.get_ident())
        self.key_frames.append(self.pressed_keys())
        super()._one_frame()
        if self.sink:
            self.sink(b"\0\0\0\0")


@pytest.fixture
def actor():
    backend = StreamingFake()
    clock = EmuClock(backend, fps=120, idle_frames=False)
    with EmulatorActor(backend, clock=clock, realtime_idle_while_deciding=True) as actor:
        yield actor, backend


def wait_frames(actor, count=5):
    """Observe actual idle ticks, without submitting a frame-advancing command."""
    target = actor.frames_run + count
    deadline = time.monotonic() + 2
    while actor.frames_run < target:
        assert time.monotonic() < deadline
        threading.Event().wait(0.001)


def test_config_opt_in_and_user_config_passthrough(tmp_path):
    assert not config_from_mapping({}).realtime_idle_while_deciding
    raw = {"emulator": {"realtime_idle_while_deciding": True}}
    assert config_from_mapping(raw).realtime_idle_while_deciding
    path = tmp_path / "pokegent.yaml"
    path.write_text("decider: first\nemulator:\n  realtime_idle_while_deciding: true\n")
    loaded = load_config_file(path)
    loaded.pop("hybrid", None)  # run_from_state hands this section to HybridDecider separately
    assert config_from_mapping(loaded).realtime_idle_while_deciding
    for value in ("false", 1, None):
        with pytest.raises(AppError, match="boolean"):
            config_from_mapping({"emulator": {"realtime_idle_while_deciding": value}})
    with pytest.raises(AppError, match="unknown keys"):
        config_from_mapping({"emulator": {"typo": True}})


@pytest.mark.parametrize("enabled,fps,expected_fps", [(False, None, None), (True, None, 60),
                                                      (True, 75, 75)])
def test_application_wires_scoped_clock_without_a_rom(monkeypatch, enabled, fps, expected_fps):
    monkeypatch.setattr("pokegent.emulator.PyGBAEmulator", StreamingFake)
    prefix = "pokegent.games.gen3.firered.rom.rom_data."
    monkeypatch.setattr(prefix + "load_profiles", lambda: ())
    monkeypatch.setattr(prefix + "identify_rom_file", lambda *args: None)
    monkeypatch.setattr(prefix + "require_supported", lambda *args: "synthetic-profile")
    with open_emulator("/unused.gba", fps=fps,
                       realtime_idle_while_deciding=enabled) as (actor, profile):
        assert profile == "synthetic-profile"
        assert actor.realtime_idle_while_deciding is enabled
        assert actor._clock.fps == expected_fps
        assert not actor._clock.idle_frames or fps is None and not enabled
        assert actor.frames_run == 0


def test_idle_streams_at_pace_with_no_keys_and_stops_before_input(actor):
    actor, backend = actor
    audio = AudioHub()
    actor.attach_audio_sink(audio.push)
    hub = FrameHub(actor.reader())
    before = actor.frames_run
    time.sleep(0.025)
    assert actor.frames_run == before  # default scoped clock stays frozen outside decisions
    started = time.monotonic()
    with actor.decision_wait(lambda sample: True) as idle:
        first = hub.grab(max_age=0)
        wait_frames(actor, 8)
        second = hub.grab(max_age=0)
        assert second.emu_frame > first.emu_frame
        assert audio.pushed >= 8
    assert not idle.changed.is_set()
    assert time.monotonic() - started >= 7 / 120 - 0.015
    assert set(backend.key_frames) == {frozenset()}
    stopped = actor.frames_run
    time.sleep(0.025)
    assert actor.frames_run == stopped
    # Existing commands still use exact frame counts and FIFO, including hold/release frames.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = actor._clock.submit_press(("a",), 3, 2)
        f2 = actor._clock.submit_press(("l",), 2, 1)
        assert pool.submit(f1.result, 2).result() == stopped + 5
        assert pool.submit(f2.result, 2).result() == stopped + 8
    assert backend.read_u32(0x02000004) == 3
    assert backend.read_u32(0x02000008) == 2
    assert len(backend.thread_ids) == 1
    assert not backend.pressed_keys()


@pytest.mark.parametrize("mode", ["change", "aba", "read_failure", "exception"])
def test_idle_latches_transitions_and_always_stops(actor, mode):
    actor, backend = actor
    start = actor.frames_run

    def guard(sample):
        if mode == "read_failure" and sample.frame > start:
            raise ValueError("unreadable RAM")
        return mode not in {"change", "aba"} or sample.frame != start + 1

    try:
        with actor.decision_wait(guard) as idle:
            wait_frames(actor)
            if mode == "exception":
                raise RuntimeError("provider failed")
    except RuntimeError:
        assert mode == "exception"
    assert idle.changed.is_set() is (mode != "exception")
    stopped = actor.frames_run
    time.sleep(0.02)
    assert actor.frames_run == stopped
    assert not backend.pressed_keys()


def test_observable_identity_ignores_clocks_but_detects_all_scene_facts():
    read = composite()
    expected = observable_state(read)
    snapshot = read.snapshot
    ui = snapshot.ui
    player = snapshot.player.model_copy(update={
        "play_time": PlayTime(hours=0, minutes=1, seconds=2),
    })
    timed = snapshot.model_copy(update={"player": player, "snapshot_seq": 99,
                                       "emulator_frame": 99, "state_hash": "different",
                                       "ui": ui.model_copy(update={"main": ui.main.model_copy(
                                           update={"held_keys": 1, "new_keys": 1})})})
    assert observable_state(replace(read, snapshot=timed)) == expected
    assert observable_state(change_observable(read, "npc_movement")) == expected
    variants = [
        snapshot.model_copy(update={"player": snapshot.player.model_copy(update={"x": 1})}),
        snapshot.model_copy(update={"player": snapshot.player.model_copy(update={"money": 1})}),
        snapshot.model_copy(update={"vars": VarTable(nonzero=((0x4000, 1),))}),
        snapshot.model_copy(update={"ui": ui.model_copy(update={"cursor": 1})}),
        snapshot.model_copy(update={"ui": ui.model_copy(update={"main": ui.main.model_copy(
            update={"state": 2})})}),
        snapshot.model_copy(update={"ui": ui.model_copy(update={"script": ui.script.model_copy(
            update={"script_ptr": 0x08000011})})}),
        snapshot.model_copy(update={"map": snapshot.map.model_copy(update={"flash_level": 1})}),
        snapshot.model_copy(update={"battle": read.battle.model_copy(update={"turn": 1})}),
    ]
    for changed in variants:
        assert observable_state(replace(read, snapshot=changed)) != expected
    text = DialogText(pages=("A synthetic page",), source="printing")
    first_page = replace(read, text=text)
    assert observable_state(first_page) != expected
    assert observable_state(replace(first_page, text=replace(text, page_index=1))) != (
        observable_state(first_page)
    )
    with pytest.raises(ValueError):
        observable_state(replace(read, snapshot=snapshot.model_copy(update={"map": None})))


def test_live_forced_player_step_is_stale_even_before_saved_position_changes():
    read = composite()
    placement = PlayerPlacement(
        save_x=0, save_y=0, avatar_x=0, avatar_y=0, prev_x=0, prev_y=0, x=0, y=0,
        position_consistent=True, facing="down", elevation=0, object_event_id=0,
        avatar_flags=0, surfing=False, underwater=False, biking=False,
        controllable_flag=False, forced=True, moving=False,
        tile_transition_state=0, running_state=0,
    )
    before = replace(read, snapshot=read.snapshot.model_copy(update={"map":
        read.snapshot.map.model_copy(update={"player": placement})}))
    after = replace(read, snapshot=read.snapshot.model_copy(update={"map":
        read.snapshot.map.model_copy(update={"player": placement.model_copy(update={
            "avatar_x": 1, "moving": True, "position_consistent": False,
        })})}))
    assert after.snapshot.player == before.snapshot.player
    assert observable_state(after) != observable_state(before)


def synthetic_source(actor, monkeypatch, *, transition=False):
    profile = RomProfile.model_validate(dict(
        profile_id="synthetic", display_name="Synthetic", supported=True,
        rom=dict(sha1="0" * 40, game_code="TEST", version=0),
        structs={"Dummy": dict(size=1, fields={}, source="synthetic")},
        symbols={"dummy": dict(address=0x02001000, source="synthetic")},
    ))
    baseline = composite()
    atomic = actor.read_atomic_snapshot()

    def build(self, sample):
        # The synthetic transition depends on the fake's frame word, like the real decoder's
        # tracked RAM reads. Full decoder/prefilter coverage is tested separately below.
        sample.read_range(0x02000000, 4)
        snapshot = baseline.snapshot.model_copy(update={
            "snapshot_seq": sample.frame + sample.load_epoch,
            "emulator_frame": sample.frame,
        })
        self.last = replace(baseline, snapshot=snapshot, atomic_frame=sample.frame)
        if transition and sample.frame > atomic.frame:
            # None of these changes alter the deliberately narrow builder fingerprint below.
            self.last = change_observable(self.last, transition)
        return snapshot

    monkeypatch.setattr(CompositeSnapshotSource, "build", build)
    source = CompositeSnapshotSource(actor, profile)
    source._last_atomic = atomic
    source.build(atomic)
    return source


@pytest.mark.parametrize("transition", [
    False, "menu_cursor", "dialog_page", "npc_movement", "map_change", "script", "battle_phase",
])
def test_runner_rejects_idle_change_or_passes_through_ram_verifier(actor, monkeypatch, transition):
    actor, backend = actor
    source = synthetic_source(actor, monkeypatch, transition=transition)
    frame = decision()
    events = MemoryEventLog("synthetic")
    registry, verifiers = ExecutorRegistry(), VerifierRegistry()
    registry.register(ExecutorSpec("test.confirm", ExecutionKind.ATOMIC,
                                   lambda ctx: Tap("a", hold_frames=2, wait_frames=1)))
    verifications = []

    def perceive(**kwargs):
        sample = actor.read_atomic_snapshot()
        presses = sample.read_u32(0x02000004)
        snapshot = source.last.snapshot.model_copy(update={
            "snapshot_seq": sample.frame, "emulator_frame": sample.frame,
            "player": source.last.snapshot.player.model_copy(update={"money": presses}),
        })
        return Perception(PerceptionKind.READY, snapshot,
                          SceneResult(scene_id=frame.scene_id, confidence=1), frame)

    verifiers.register(VerifierSpec("test.pressed", lambda ctx: (
        verifications.append(ctx.after.player.money) or ctx.after.player.money == 2
    )))
    def observe(*args, **kwargs):
        view = perceive()
        return Observation(view.snapshot, view.scene)

    perceiver = SimpleNamespace(source=source, perceive=perceive, observe=observe,
                                bind_settle=lambda fn: None)
    executor = Executor(actor, registry, verifiers, perceiver)
    decider = SimpleNamespace(decide=lambda f: wait_frames(actor) or ChoiceResult(
        option_id="confirm", decision_id=f.decision_id))
    discarded = []
    decider.discard_stale_decision = lambda: discarded.append(True)
    runner = Runner(decider=decider, executor=executor, events=events, perceiver=perceiver)
    chosen = runner._decide(frame)
    if transition and transition != "npc_movement":
        assert chosen is None
        assert events.events[-1].type == EventType.OPTION_REJECTED
        assert events.events[-1].outcome == ErrorCode.E_STALE_DECISION.value
        assert backend.read_u32(0x02000004) == 0
        assert discarded == [True]
        # The next fresh frame is accepted; the obsolete frame is never used for input.
        source._last_atomic = actor.read_atomic_snapshot()
        source.build(source._last_atomic)
        assert runner._decide(decision(actor.frames_run)) is not None
    else:
        assert chosen == (frame, "confirm")
        stopped = actor.frames_run
        result = executor.execute(*chosen)
        assert result.status.value == "applied", result
        assert result.verified_outcome is True
        assert result.elapsed_frames == 3
        assert actor.frames_run == stopped + 3
        assert verifications and verifications[-1] == 2
        assert backend.read_u32(0x02000004) == 2


def test_external_stale_choice_uses_http_409_and_unblocks_for_fresh_decision(monkeypatch):
    # Bind the ordinary HTTP implementation to an OS-selected local port for this ROM-free test.
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr("pokegent.server.ThreadingHTTPServer",
                        lambda address, handler: ThreadingHTTPServer((address[0], 0), handler))
    decider = ExternalDecider()
    published = threading.Event()
    valid = threading.Event()
    valid.set()
    decider.bind_decision_valid(valid.is_set)
    decider.bind_agent_request(lambda *args: published.set())
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(decider.decide, decision())
            assert published.wait(2)
            old = decider._frame["frame_id"]
            valid.clear()
            body = json.dumps({"frame_id": old, "option_id": "confirm"}).encode()
            request = Request(f"http://127.0.0.1:{decider.port}/choose", data=body,
                              headers={"Content-Type": "application/json"})
            with pytest.raises(HTTPError) as rejected:
                urlopen(request, timeout=2)
            assert rejected.value.code == 409
            with pytest.raises(StaleDecision):
                pending.result(timeout=2)
            valid.set()
            published.clear()
            fresh = pool.submit(decider.decide, decision(1))
            assert published.wait(2)
            assert decider._frame["frame_id"] != old
            decider.choose({"frame_id": decider._frame["frame_id"], "option_id": "confirm"})
            assert fresh.result(timeout=2).decision_id == "choice-1"
            # A final idle frame can invalidate an already acknowledged external choice.
            # It must not be adopted on a later hybrid System One decision.
            assert decider._decided_frame is not None
            decider.discard_stale_decision()
            assert decider.take_decided_frame() is None
            assert not decider._agent_choice_pending
            assert decider._outcome["status"] == "stale"
            assert decider._outcome["option_id"] == "confirm"
            published.clear()
            fresh = pool.submit(decider.decide, decision(2))
            assert published.wait(2)
            assert decider._feedback[-1]["status"] == "stale"
            decider.choose({"frame_id": decider._frame["frame_id"], "option_id": "confirm"})
            fresh.result(timeout=2)
    finally:
        decider.close()


@pytest.mark.parametrize("limit", [1, 3])
def test_stale_budget_freezes_next_fresh_decision(actor, monkeypatch, limit):
    actor, backend = actor
    source = synthetic_source(actor, monkeypatch, transition="menu_cursor")
    source.decision_guard = lambda *args, **kwargs: lambda sample: False
    calls = []

    def decide(frame):
        calls.append(frame)
        # Frozen fallback must not tick, and no guard/provider thread can send input.
        if len(calls) <= limit:
            wait_frames(actor)
        return ChoiceResult(option_id="confirm")

    runner = Runner(decider=SimpleNamespace(decide=decide),
                    executor=SimpleNamespace(actor=actor, perceiver=True),
                    events=MemoryEventLog("synthetic"),
                    perceiver=SimpleNamespace(source=source),
                    config=RunnerConfig(decision_idle_stale_limit=limit))
    # Scripted state/fingerprint changes must not renew the same decision point's budget.
    for n in range(limit):
        source._last_atomic = actor.read_atomic_snapshot()
        assert runner._decide(decision(source._last_atomic.frame)) is None
    source._last_atomic = actor.read_atomic_snapshot()
    before = actor.frames_run
    frame = decision(before)
    assert runner._decide(frame) == (frame, "confirm")
    assert actor.frames_run == before
    assert runner.counters.stale_decisions == limit
    assert runner.counters.frozen_decisions == 1
    assert not backend.read_u32(0x02000004)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_stale_limit_is_a_positive_integer(value):
    with pytest.raises(ValueError, match="decision_idle_stale_limit"):
        config_from_mapping({"runner": {"decision_idle_stale_limit": value}}).runner_config()


def test_guard_time_has_separate_metrics():
    now = [0.0]

    class Backend:
        def tick(self, n):
            now[0] += .002

    clock = EmuClock(Backend(), timer=lambda: now[0])
    clock._decision_idle = lambda backend: now.__setitem__(0, now[0] + .005)
    clock._frame()
    metrics = clock.metrics()
    assert metrics["frame_ms"]["mean"] == pytest.approx(2)
    assert metrics["decision_guard_ms"]["mean"] == pytest.approx(5)


def real_decoder_source():
    """Invented pre-game RAM, with the real composite/UI/core readers and no game data."""
    from test_intro_save_validation import synthetic_save

    from pokegent.games.gen3.firered.reader import ui_symbols as U

    reader = synthetic_save(U.NEW_GAME_SCENE_CALLBACK)
    profile = reader.profile.model_dump()
    addresses = dict(
        gTasks=0x02008000, sTextPrinters=0x02009000, gWindows=0x0200A000,
        sGlobalScriptContextStatus=0x0200B000, sGlobalScriptContext=0x0200B010,
        sLockFieldControls=0x0200B100, sMenu=0x0200C000, sYesNoWindowId=0x0200C100,
        gMapHeader=0x02001F00, VMap=0x02001E00, gBackupMapData=0x02001D00,
        gMapGroups=0x08000800,
    )
    for name, address in addresses.items():
        profile["symbols"][name] = dict(address=address, source="synthetic")
    profile["structs"]["SaveBlock1"]["fields"]["flashLevel"] = dict(
        offset=0, size=1, source="synthetic")
    profile["structs"]["MapHeader"] = dict(size=28, source="synthetic", fields={
        name: dict(offset=i * 4, size=4, source="synthetic")
        for i, name in enumerate(("mapLayout", "events", "mapScripts", "connections"))
    })
    profile = RomProfile.model_validate(profile)
    atomic = reader._source.read_atomic_snapshot()
    # An ordinary unknown background task, with changing work counters.
    atomic = ram_change(atomic, addresses["gTasks"], b"\x01\x09\x00\x08\x01\x00\x00\x00")
    source = CompositeSnapshotSource(SimpleNamespace(read_atomic_snapshot=lambda: atomic), profile)
    source.build(atomic)
    assert source.last.snapshot.health.valid and not source.last.snapshot.ui.read_errors
    return source, atomic


def ram_change(atomic, address, data):
    regions = dict(atomic.regions)
    for region in atomic.region_defs:
        if region.contains(address, len(data)):
            raw = bytearray(regions[region.name])
            raw[address - region.base:address - region.base + len(data)] = data
            regions[region.name] = bytes(raw)
            return replace(atomic, regions=regions, frame=atomic.frame + 1)
    raise AssertionError(address)


def test_byte_prefilter_uses_real_decoder_and_ignores_background_counters(monkeypatch):
    from pokegent.games.gen3.firered.reader import ui_symbols as U

    source, atomic = real_decoder_source()
    builds = []
    original = CompositeSnapshotSource.build

    def counted(self, sample):
        builds.append(sample.frame)
        return original(self, sample)

    monkeypatch.setattr(CompositeSnapshotSource, "build", counted)
    guard = source.decision_guard(atomic.frame + atomic.load_epoch)
    assert len(builds) == 1
    for address in (0x02008000 + U.TASK_DATA, 0x02002024 + 0x10,
                    0x03000200 + U.MAIN_HELD_KEYS, 0x0203F000):
        atomic = ram_change(atomic, address, b"\x02")
        assert guard(atomic)
    assert len(builds) == 1  # no model dumps, map reads or hashing on unchanged relevant RAM
    changed = ram_change(atomic, 0x0200C000 + U.SMENU_CURSOR_POS, b"\x01")
    assert not guard(changed)
    assert len(builds) == 2
    assert not guard(replace(atomic, load_epoch=atomic.load_epoch + 1))


@pytest.mark.parametrize("field", ["money", "flags", "vars", "pos"])
def test_real_decoder_prefilter_never_hides_core_changes(field):
    source, atomic = real_decoder_source()
    guard = source.decision_guard(atomic.frame + atomic.load_epoch)
    offset = source.profile.field("SaveBlock1", field).offset
    if field == "flags":
        offset += 1  # flag zero is reserved and intentionally absent from decoded flags
    assert not guard(ram_change(atomic, 0x02004000 + offset, b"\x01"))
