"""Game-owned story fallback composition (#139).

Synthetic progression artifacts only: the real loader, identity seam and session revalidation
are exercised with invented inputs. No ROM, actor read, game service or native call is involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_story_source import load

import pokegent.app as app_module
import pokegent.games.gen3.firered.plugin as plugin_module
import pokegent.games.gen3.firered.story as story_module
import pokegent.goals.story as goals_story_module
import pokegent.goals.story_identity as identity_module
from pokegent.app import (
    EXIT_ENVIRONMENT,
    AppConfig,
    AppError,
    build_runner,
    make_objective_session,
)
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import PluginRefusal, RefusalCode
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore

KNOWN_ROM = b"synthetic-firered-story-factory-known" * 8
REDACTED = "^story fallback requires valid private progression data$"


def _profile():
    """The real FireRed profile with only the invented test digest changed."""
    base = load_profiles().get("firered-us-rev0")
    digest = hashlib.sha1(KNOWN_ROM).hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": digest})})


class Recorder:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FalseyRecorder(Recorder):
    def __bool__(self) -> bool:
        return False


def _goal(tmp_path):
    return tmp_path / "goal.json"


def test_default_helpers_use_the_existing_lazy_seams(tmp_path, monkeypatch):
    load(tmp_path)
    real_load = goals_story_module.load_progression
    load_calls, verify_calls = [], []

    def load_once(*args, **kwargs):
        load_calls.append((args, kwargs))
        return real_load(*args, **kwargs)

    def verify_once(*args, **kwargs):
        verify_calls.append((args, kwargs))

    monkeypatch.setattr(goals_story_module, "load_progression", load_once)
    monkeypatch.setattr(identity_module, "verify_identity", verify_once)
    story_path = tmp_path / "progression.json"

    session = make_objective_session(
        _goal(tmp_path), story_fallback=True, story_path=str(story_path),
        rom_path="rom-token",
    )

    assert load_calls == [((), {"path": story_path, "directory": None})]
    assert session.fallback is not None
    assert verify_calls == [((session.fallback.graph, "rom-token"), {})]


def test_injected_factory_and_validator_run_once_with_exact_arguments(tmp_path):
    source = load(tmp_path)
    directory = SimpleNamespace(name="objective-directory")
    factory, validator = Recorder(source), Recorder(None)
    story_path = tmp_path / "progression.json"

    session = make_objective_session(
        _goal(tmp_path), story_fallback=True, story_path=str(story_path),
        rom_path="rom-token", directory=directory,
        story_fallback_factory=factory, story_identity_validator=validator,
    )

    assert factory.calls == [((story_path, directory), {})]
    assert validator.calls == [((source.graph, "rom-token"), {})]
    assert session.fallback is source


def test_falsey_callables_are_used_rather_than_replaced(tmp_path):
    source = load(tmp_path)
    factory, validator = FalseyRecorder(source), FalseyRecorder(None)
    assert not factory and not validator

    session = make_objective_session(
        _goal(tmp_path), story_fallback=True, story_path=tmp_path / "progression.json",
        story_fallback_factory=factory, story_identity_validator=validator,
    )

    assert len(factory.calls) == 1
    assert len(validator.calls) == 1
    assert session.fallback is source


def test_disabled_flag_never_touches_story_helpers(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("a disabled story flag used a story helper")

    monkeypatch.setattr(goals_story_module, "load_progression", fail)
    monkeypatch.setattr(identity_module, "verify_identity", fail)

    session = make_objective_session(_goal(tmp_path))

    assert session is not None and session.fallback is None


def test_prebuilt_story_revalidated_with_flag_off_uses_injected_validator(tmp_path):
    source = load(tmp_path)
    session = ObjectiveSession(ObjectiveStore(_goal(tmp_path)), fallback=source)
    validator = Recorder(None)

    returned = make_objective_session(
        session, rom_path="rom-token", story_identity_validator=validator,
    )

    assert returned is session
    assert validator.calls == [((source.graph, "rom-token"), {})]


def test_prebuilt_story_default_verifier_stays_patchable_with_flag_off(tmp_path, monkeypatch):
    source = load(tmp_path)
    session = ObjectiveSession(ObjectiveStore(_goal(tmp_path)), fallback=source)
    verify_calls = []

    def verify_once(*args, **kwargs):
        verify_calls.append((args, kwargs))

    monkeypatch.setattr(identity_module, "verify_identity", verify_once)

    assert make_objective_session(session, rom_path="rom-token") is session
    assert verify_calls == [((source.graph, "rom-token"), {})]


@pytest.mark.parametrize("error", [ValueError("secret path"), TypeError("secret path")])
def test_factory_errors_keep_environment_priority_and_redaction(tmp_path, error):
    def factory(*args, **kwargs):
        raise error

    with pytest.raises(AppError) as caught:
        make_objective_session(
            _goal(tmp_path), story_fallback=True, story_path="x.json",
            story_fallback_factory=factory,
        )

    assert caught.value.exit_code == EXIT_ENVIRONMENT
    assert str(caught.value) == "story fallback requires valid private progression data"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("error", [ValueError("secret bytes"), TypeError("secret bytes")])
def test_validator_errors_keep_environment_priority_and_redaction(tmp_path, error):
    def validator(*args, **kwargs):
        raise error

    with pytest.raises(AppError) as caught:
        make_objective_session(
            _goal(tmp_path), story_fallback=True, story_path=tmp_path / "progression.json",
            story_identity_validator=validator,
        )

    assert caught.value.exit_code == EXIT_ENVIRONMENT
    assert str(caught.value) == "story fallback requires valid private progression data"


@pytest.mark.parametrize("bad", [False, 7, object()])
def test_bad_story_paths_stay_redacted(tmp_path, bad):
    with pytest.raises(AppError, match=REDACTED) as caught:
        make_objective_session(_goal(tmp_path), story_fallback=True, story_path=bad)
    assert caught.value.exit_code == EXIT_ENVIRONMENT


def test_story_and_dex_mutual_exclusion_precedes_helpers(tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("mutual exclusion reached a story helper")

    with pytest.raises(AppError, match="^story fallback cannot combine with dex$"):
        make_objective_session(
            _goal(tmp_path), dex_fallback=True, story_fallback=True,
            story_fallback_factory=fail,
        )


def test_compose_names_story_callables_without_invoking_them(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("compose must only name the supplied callables")

    monkeypatch.setattr(story_module, "make_story_fallback", fail)
    monkeypatch.setattr(story_module, "verify_story_identity", fail)
    services = FireRedPlugin(_profile()).compose()

    assert services.story_fallback_factory is fail
    assert services.story_identity_validator is fail
    assert "story_fallback" in FIRERED_CAPABILITIES


class ReachedFactory(Exception):
    pass


def _record_admission(monkeypatch):
    seen = {}

    def admission(*args, **kwargs):
        seen.update(kwargs)
        seen["args"] = args
        raise ReachedFactory

    monkeypatch.setattr(app_module, "make_objective_session", admission)
    return seen


def test_admitted_story_build_passes_plugin_callables(monkeypatch, tmp_path):
    def story_factory(*args, **kwargs):
        raise AssertionError("the app invoked the plugin factory")

    def story_validator(*args, **kwargs):
        raise AssertionError("the app invoked the plugin validator")

    monkeypatch.setattr(story_module, "make_story_fallback", story_factory)
    monkeypatch.setattr(story_module, "verify_story_identity", story_validator)
    seen = _record_admission(monkeypatch)

    with pytest.raises(ReachedFactory):
        build_runner(
            actor=object(), profile=_profile(), events=object(),
            config=AppConfig(run_id="story-factory", map_mode="always", story_fallback=True,
                             on_objective_done="continue"),
            objectives=_goal(tmp_path), experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert seen["story_fallback"] is True
    assert seen["story_fallback_factory"] is story_factory
    assert seen["story_identity_validator"] is story_validator


def test_default_off_build_passes_no_story_callables(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("compose must not invoke the story callables")

    monkeypatch.setattr(story_module, "make_story_fallback", fail)
    monkeypatch.setattr(story_module, "verify_story_identity", fail)
    seen = _record_admission(monkeypatch)

    with pytest.raises(ReachedFactory):
        build_runner(
            actor=object(), profile=_profile(), events=object(),
            config=AppConfig(run_id="story-factory", map_mode="always"),
            objectives=_goal(tmp_path), experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert seen["story_fallback"] is False
    assert seen["story_fallback_factory"] is None
    assert seen["story_identity_validator"] is None


class MissingStoryCapabilityPlugin(FireRedPlugin):
    @property
    def descriptor(self):
        return replace(
            super().descriptor,
            capabilities=FIRERED_CAPABILITIES - {"story_fallback"},
        )

    def compose(self):
        raise AssertionError("a plugin missing story_fallback reached compose")


def test_missing_story_capability_refuses_before_compose_and_actor(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module, "FireRedPlugin", MissingStoryCapabilityPlugin)
    admission = _record_admission(monkeypatch)

    with pytest.raises(PluginRefusal) as caught:
        build_runner(
            actor=object(), profile=_profile(), events=object(),
            config=AppConfig(run_id="story-factory", map_mode="always", story_fallback=True,
                             on_objective_done="continue"),
            objectives=_goal(tmp_path), experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert admission == {}
