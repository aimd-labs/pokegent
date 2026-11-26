"""Plugin-owned pre-game callback service (#139 follow-up), RED-first.

Synthetic exact-ROM bytes only. The selected service must only narrow FireRed's existing
invalid-core admission; these tests touch no ROM file, actor read or provider.
"""

from __future__ import annotations

import hashlib

import pytest

from pokegent.actions.menu.system_flow.symbols import PRE_GAME_CALLBACKS
from pokegent.app import AppConfig, NewGameConfig, build_runner
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.reader.schemas import (
    Health,
    ReadError,
    StateSnapshot,
)
from pokegent.games.gen3.firered.reader.ui_schemas import (
    FieldEvidence,
    FnRef,
    MainEvidence,
    MenuEvidence,
    ScriptEvidence,
    TaskEvidence,
    TextEvidence,
    UiEvidence,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    RefusalCode,
    ReviewedPluginRegistry,
)
from pokegent.state.catalog import SCENE_INVALID
from pokegent.state.scenes import DetectorRegistry, PriorityClass, SceneResult

KNOWN_ROM = b"synthetic-firered-pre-game-callbacks-known" * 8
SUBSET = frozenset({"CB2_MainMenu"})
UNRELATED = "CB2_BagMenuRun"


def _test_profile():
    base = load_profiles().get("firered-us-rev0")
    return base.model_copy(
        update={
            "rom": base.rom.model_copy(
                update={"sha1": hashlib.sha1(KNOWN_ROM).hexdigest()}
            )
        }
    )


class NoReadActor:
    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched before admission/composition")


class _Services:
    """Delegates every real service except the callback set under test."""

    _missing = object()

    def __init__(self, real, value=_missing) -> None:
        self._real = real
        self._value = value

    def __getattr__(self, name):
        if name == "pre_game_callbacks":
            if self._value is _Services._missing:
                raise AttributeError(name)
            return self._value
        return getattr(self._real, name)


class _WrappedPlugin(FireRedPlugin):
    def __init__(self, profile, value) -> None:
        super().__init__(profile)
        self._value = value

    def compose(self):
        return _Services(super().compose(), self._value)


class _MissingCapabilityPlugin(FireRedPlugin):
    @property
    def descriptor(self):
        return GamePluginDescriptor(
            plugin_id=super().descriptor.plugin_id,
            game=super().descriptor.game,
            profile_id=super().descriptor.profile_id,
            contract_version=super().descriptor.contract_version,
            rom_sha1=super().descriptor.rom_sha1,
            capabilities=frozenset(FIRERED_CAPABILITIES) - {"pre_game_callbacks"},
        )

    def compose(self):
        raise AssertionError("missing pre_game_callbacks capability reached compose")


class _NoComposePlugin(FireRedPlugin):
    def compose(self):
        raise AssertionError("refused admission reached compose")


def _registry(plugin):
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    return registry


def _build(profile, *, plugin=None, new_game=False, actor=None):
    actor = actor if actor is not None else NoReadActor()
    kwargs = {}
    if plugin is not None:
        kwargs = {
            "experimental_game_plugin": True,
            "plugin_rom_bytes": KNOWN_ROM,
            "plugin_registry": _registry(plugin),
        }
    config = AppConfig(
        run_id="plugin-pre-game",
        map_mode="always",
        new_game=NewGameConfig(enabled=new_game, gender="male"),
    )
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        config=config,
        **kwargs,
    )


def test_selected_service_subset_reaches_perceiver():
    plugin = _WrappedPlugin(_test_profile(), SUBSET)

    app = _build(_test_profile(), plugin=plugin, new_game=True)

    assert app.perceiver._pre_game_mode is True
    assert app.perceiver._pre_game_callbacks == SUBSET
    assert app.perceiver._pre_game_callbacks < PRE_GAME_CALLBACKS


def test_empty_service_stays_empty_and_fail_closed():
    plugin = _WrappedPlugin(_test_profile(), frozenset())

    app = _build(_test_profile(), plugin=plugin, new_game=True)

    assert app.perceiver._pre_game_mode is True
    assert app.perceiver._pre_game_callbacks == frozenset()


def test_builtin_plugin_keeps_the_original_firered_set():
    profile = _test_profile()
    app = _build(
        profile,
        plugin=FireRedPlugin(profile),
        new_game=True,
    )

    assert app.perceiver._pre_game_mode is True
    assert app.perceiver._pre_game_callbacks == PRE_GAME_CALLBACKS


def test_legacy_new_game_path_keeps_the_original_firered_set():
    app = _build(_test_profile(), new_game=True)

    assert app.perceiver._pre_game_mode is True
    assert app.perceiver._pre_game_callbacks == PRE_GAME_CALLBACKS


def test_default_new_game_off_never_accesses_the_service():
    plugin = _WrappedPlugin(_test_profile(), _Services._missing)

    app = _build(_test_profile(), plugin=plugin, new_game=False)

    assert app.perceiver._pre_game_mode is False
    assert app.perceiver._pre_game_callbacks == frozenset()


def test_missing_capability_refuses_before_compose(monkeypatch):
    import pokegent.games.gen3.firered.plugin as plugin_module

    monkeypatch.setattr(plugin_module, "FireRedPlugin", _NoComposePlugin)

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            plugin=_MissingCapabilityPlugin(_test_profile()),
            new_game=True,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY


@pytest.mark.parametrize(
    "value",
    [
        _Services._missing,
        ["CB2_MainMenu"],
        "CB2_MainMenu",
        frozenset({"CB2_MainMenu", 3}),
        frozenset({"CB2_MainMenu", "Not_A_FireRed_Callback"}),
    ],
    ids=["absent", "list", "string", "non-plain-entry", "unknown-callback"],
)
def test_invalid_service_refuses_before_actor(value, monkeypatch):
    import pokegent.games.gen3.firered.composition as composition_module

    def fail(*args, **kwargs):
        raise AssertionError("invalid service reached menu composition")

    monkeypatch.setattr(composition_module, "make_menu_services", fail)
    actor = NoReadActor()
    plugin = _WrappedPlugin(_test_profile(), value)

    with pytest.raises(PluginRefusal) as caught:
        _build(_test_profile(), plugin=plugin, new_game=True, actor=actor)

    assert caught.value.code in {
        RefusalCode.MISSING_CAPABILITY,
        RefusalCode.PROFILE_MISMATCH,
    }
    assert actor.touched == []


class _PreGameDetector:
    name = "synthetic-pre-game"
    priority = PriorityClass.MENU
    specificity = 1
    accepts_pre_game_core = True

    def detect(self, snapshot):
        return SceneResult(scene_id="menu.open", confidence=1.0)


def _invalid_core_snapshot(callback_name):
    callback = FnRef(ptr=0x08000001, name=callback_name, verified=True)
    ui = UiEvidence(
        valid=True,
        callback_id=callback_name,
        main=MainEvidence(valid=True, confidence=1.0, callback2=callback),
        tasks=TaskEvidence(valid=True, confidence=1.0),
        text=TextEvidence(valid=True, confidence=1.0),
        script=ScriptEvidence(
            valid=True, confidence=1.0, running=False, controls_locked=False
        ),
        menu=MenuEvidence(valid=True, confidence=1.0),
        field=FieldEvidence(
            valid=True, confidence=1.0, fade_active=False, prevent_step=False
        ),
    )
    return StateSnapshot.create(
        rom_profile="firered-us-rev0",
        snapshot_seq=0,
        health=Health(
            valid=False,
            read_errors=(
                ReadError(subsystem="save", code="invalid", message="synthetic"),
            ),
        ),
        ui=ui,
    )


def test_unrelated_callback_cannot_bypass_fail_closed_invalid_core():
    registry = DetectorRegistry([_PreGameDetector()])
    allowed = _invalid_core_snapshot("CB2_MainMenu")
    unrelated = _invalid_core_snapshot(UNRELATED)

    claimed = registry.detect_all(
        allowed, pre_game_callbacks=frozenset({"CB2_MainMenu"})
    )
    refused = registry.detect_all(
        unrelated, pre_game_callbacks=frozenset({"CB2_MainMenu"})
    )
    empty = registry.detect_all(allowed, pre_game_callbacks=frozenset())

    assert claimed.result.scene_id == "menu.open"
    assert claimed.result.core_invalid is True
    assert refused.result.scene_id == SCENE_INVALID
    assert empty.result.scene_id == SCENE_INVALID
