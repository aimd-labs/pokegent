"""Synthetic ROM admission and fake backends only; no native mGBA, ROM or provider calls."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pokegent.app import AppError, open_emulator
from pokegent.emulator import EmulatorActor, InputRejected
from pokegent.games.gen3.firered.rom.rom_data import UnsupportedRomError
from pokegent.testing.fake_emulator import FakeGBAEmulator

ADMISSION = "pokegent.games.gen3.firered.rom.rom_data."


def patch_admission(monkeypatch, events, *, profile="synthetic-profile", unsupported=False):
    def load_profiles():
        events.append("profiles")
        return ()

    def identify_rom_file(rom_path, profiles):
        events.append("identify")
        return None

    def require_supported(identification, profiles):
        events.append("require")
        if unsupported:
            raise UnsupportedRomError(SimpleNamespace(
                supported=False, profile_id=None,
                status=SimpleNamespace(value="unsupported"), reasons=("synthetic",)))
        return profile

    monkeypatch.setattr(ADMISSION + "load_profiles", load_profiles)
    monkeypatch.setattr(ADMISSION + "identify_rom_file", identify_rom_file)
    monkeypatch.setattr(ADMISSION + "require_supported", require_supported)


class RecordingBackend(FakeGBAEmulator):
    def __init__(self, events, *, fail_load=False, fail_close=False):
        super().__init__()
        self.events = events
        self.close_count = 0
        self.loaded_path = None
        self.fail_load = fail_load
        self.fail_close = fail_close

    def load(self, rom_path):
        self.events.append("load")
        if self.fail_load:
            raise RuntimeError("synthetic load failure")
        self.loaded_path = rom_path
        super().load(rom_path)

    def close(self):
        self.close_count += 1
        self.events.append("close")
        if self.fail_close:
            raise RuntimeError("synthetic close failure")
        super().close()


def factory_for(backend, events):
    def factory():
        events.append("factory")
        return backend

    return factory


def test_callback_order_is_admission_then_factory_then_load(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)
    with open_emulator(
        "/unused.gba", experimental_backend=True,
        backend_factory=factory_for(backend, events),
    ) as (actor, profile):
        assert isinstance(actor, EmulatorActor)
        assert profile == "synthetic-profile"
    assert events == ["profiles", "identify", "require", "factory", "load", "close"]
    assert backend.loaded_path == "/unused.gba"


@pytest.mark.parametrize("case", [
    "factory_without_flag", "flag_without_factory", "non_bool", "not_callable",
])
def test_invalid_experimental_gate_refuses_before_admission(monkeypatch, case):
    events = []
    patch_admission(monkeypatch, events)
    calls = []

    def factory():
        calls.append("factory")
        return RecordingBackend(events)

    kwargs = {
        "factory_without_flag": {"backend_factory": factory},
        "flag_without_factory": {"experimental_backend": True},
        "non_bool": {"experimental_backend": 1, "backend_factory": factory},
        "not_callable": {"experimental_backend": True, "backend_factory": 42},
    }[case]
    with pytest.raises(AppError):
        with open_emulator("/unused.gba", **kwargs):
            pass
    assert events == []
    assert calls == []


def test_bool_spoof_is_refused_before_admission(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    calls = []
    spoof = Mock(spec=bool)
    assert isinstance(spoof, bool)  # the mock spoofs __class__, so this is not exact typing
    with pytest.raises(AppError, match="boolean"):
        with open_emulator(
            "/unused.gba", experimental_backend=spoof,
            backend_factory=lambda: calls.append("factory") or RecordingBackend(events),
        ):
            pass
    assert events == []
    assert calls == []


def test_unsupported_rom_invokes_no_factory_or_constructor(monkeypatch):
    events = []
    patch_admission(monkeypatch, events, unsupported=True)
    backend = RecordingBackend(events)
    with pytest.raises(UnsupportedRomError):
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            pass
    assert events == ["profiles", "identify", "require"]
    assert backend.close_count == 0


def test_factory_return_must_be_an_emulator_before_load(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)

    class NotABackend:
        def load(self, rom_path):
            events.append("load")

        def close(self):
            events.append("close")

    with pytest.raises(AppError, match="Emulator"):
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=lambda: events.append("factory") or NotABackend(),
        ):
            pass
    assert events == ["profiles", "identify", "require", "factory"]


def test_default_path_constructs_only_pygba_and_closes_once(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    made = []

    class DefaultBackend(RecordingBackend):
        def __init__(self):
            super().__init__(events)
            made.append(self)

    monkeypatch.setattr("pokegent.emulator.PyGBAEmulator", DefaultBackend)
    with open_emulator("/unused.gba") as (actor, profile):
        assert isinstance(actor, EmulatorActor)
        assert profile == "synthetic-profile"
        assert events == ["profiles", "identify", "require", "load"]
    assert len(made) == 1
    assert made[0].loaded_path == "/unused.gba"
    assert made[0].close_count == 1


def test_admitted_alternate_backend_runs_behind_real_actor(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)
    with open_emulator(
        "/unused.gba", experimental_backend=True,
        backend_factory=factory_for(backend, events),
    ) as (actor, _profile):
        assert isinstance(actor, EmulatorActor)
        assert actor.advance_frames(3) == 3
        snapshot = actor.read_atomic_snapshot()
        assert snapshot.frame == backend.frame_count == 3
        assert snapshot.read_u32(0x02000000) == 3
        before = actor.frames_run
        with pytest.raises(InputRejected):
            actor.tap("not-a-button")
        assert actor.frames_run == before
        assert not backend.pressed_keys()
    assert backend.close_count == 1


@pytest.mark.parametrize("fps,realtime_idle,expected_fps", [
    (None, False, None), (75, False, 75), (None, True, 60.0),
])
def test_alternate_backend_keeps_existing_clock_selection(
    monkeypatch, fps, realtime_idle, expected_fps,
):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)
    with open_emulator(
        "/unused.gba", fps=fps, realtime_idle_while_deciding=realtime_idle,
        experimental_backend=True, backend_factory=factory_for(backend, events),
    ) as (actor, _profile):
        assert actor._clock.fps == expected_fps
        assert actor.realtime_idle_while_deciding is realtime_idle


@pytest.mark.parametrize("body_raises", [False, True])
def test_context_closes_backend_once(monkeypatch, body_raises):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)
    try:
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            if body_raises:
                raise RuntimeError("synthetic body failure")
    except RuntimeError:
        assert body_raises
    assert backend.close_count == 1


def test_load_failure_closes_once_and_keeps_original_error(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events, fail_load=True)
    with pytest.raises(RuntimeError, match="synthetic load failure"):
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            pass
    assert backend.close_count == 1
    assert events == ["profiles", "identify", "require", "factory", "load", "close"]


def test_actor_constructor_rejection_closes_loaded_backend_once(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)
    backend.button_down = None
    with pytest.raises(TypeError, match="button_down"):
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            pass
    assert backend.close_count == 1
    assert backend.frames_run == 0
    assert not backend.pressed_keys()


def test_clock_setup_failure_closes_loaded_backend_once(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events)

    class BrokenClock:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("synthetic clock setup failure")

    monkeypatch.setattr("pokegent.emulator.EmuClock", BrokenClock)
    with pytest.raises(RuntimeError, match="synthetic clock setup failure"):
        with open_emulator(
            "/unused.gba", fps=60, experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            pass
    assert backend.close_count == 1
    assert backend.frames_run == 0


def test_cleanup_failure_does_not_mask_load_failure(monkeypatch):
    events = []
    patch_admission(monkeypatch, events)
    backend = RecordingBackend(events, fail_load=True, fail_close=True)
    with pytest.raises(RuntimeError, match="synthetic load failure"):
        with open_emulator(
            "/unused.gba", experimental_backend=True,
            backend_factory=factory_for(backend, events),
        ):
            pass
    assert backend.close_count == 1
