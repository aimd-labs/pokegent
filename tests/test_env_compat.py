"""Focused tests for pokegent.env rename compatibility helpers (#389)."""

import warnings

import pytest

import pokegent.env as env


def test_reject_removed_env_and_get_env_and_system1_core():
    with pytest.raises(ValueError, match="VON_KEY"):
        env.reject_removed_env({"VON_KEY": "1"})
    with pytest.raises(ValueError, match="POKEVON_KEY"):
        env.reject_removed_env({"POKEVON_KEY": "1"})
    assert env.get_env("POKEGENT_KEY", environ={"POKEGENT_KEY": "new"}) == "new"
    assert env.get_env("POKEGENT_KEY", "fallback", environ={}) == "fallback"
    assert env.get_system1_env("SYSTEM1_API_KEY", environ={"SYSTEM1_API_KEY": "v"}) == "v"
    assert env.get_system1_env("SYSTEM1_API_KEY", "d", environ={}) == "d"


def test_get_env_falls_back_once_and_refuses_conflict(monkeypatch):
    monkeypatch.setattr(env, "_legacy_warned_names", set())
    with pytest.raises(ValueError, match="differ"):
        env.get_env("POKEGENT_KEY", environ={"POKEGENT_KEY": "a", "POKEJEV_KEY": "b"})
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert env.get_env("POKEGENT_KEY", environ={"POKEJEV_KEY": "old"}) == "old"
        assert env.get_env("POKEGENT_KEY", environ={"POKEJEV_KEY": "old"}) == "old"
    assert len(seen) == 1 and issubclass(seen[0].category, DeprecationWarning)


def test_apply_legacy_env_aliases_copies_conflicts_and_matching(monkeypatch):
    fake = {"POKEJEV_APPLY": "old"}
    monkeypatch.setattr("os.environ", fake)
    monkeypatch.setattr(env, "_legacy_warned_names", set())
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        env.apply_legacy_env_aliases()
        env.apply_legacy_env_aliases()
    assert fake["POKEGENT_APPLY"] == "old"
    assert len(seen) == 1 and issubclass(seen[0].category, DeprecationWarning)
    conflicting = {"POKEGENT_KEY": "new", "POKEJEV_KEY": "old"}
    monkeypatch.setattr("os.environ", conflicting)
    with pytest.raises(ValueError, match="differ"):
        env.apply_legacy_env_aliases()
    matching = {"POKEGENT_KEY": "same", "POKEJEV_KEY": "same"}
    monkeypatch.setattr("os.environ", matching)
    env.apply_legacy_env_aliases()
    assert matching["POKEGENT_KEY"] == "same"
