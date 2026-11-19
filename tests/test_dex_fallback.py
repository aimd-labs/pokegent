from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pokegent.app import AppError, config_from_mapping, load_config_file, make_objective_session
from pokegent.games.gen3.firered.rom.wild import Encounter
from pokegent.goals.dex_fallback import DexFallback, FallbackUnavailable
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore
from pokegent.user_config import load_user_config, to_legacy_config


def test_ranking_and_owned_exclusion_use_supplied_encounters_only():
    fallback = DexFallback((Encounter("TestA", "land", 1, .5, 2, 3),
                            Encounter("TestB", "land", 2, .25, 2, 3)))
    assert fallback.source(set())["objective_id"] == "dex_1"
    assert fallback.source({1})["objective_id"] == "dex_2"
    assert fallback.source(set(), current_map="TestB")["objective_id"] == "dex_2"
    with pytest.raises(FallbackUnavailable, match="no unowned"):
        fallback.source({1, 2})


@pytest.mark.parametrize("method,probability", [("water", .5), ("fishing", .5),
                                               ("land", 0), ("land", float("nan"))])
def test_unsupported_encounters_do_not_supply_objectives(method, probability):
    fallback = DexFallback((Encounter("TestA", method, 1, probability, 2, 3),))
    with pytest.raises(FallbackUnavailable, match="supported land encounter"):
        fallback.source(set())


def test_unknown_ram_is_not_an_empty_dex_or_healthy_party():
    fallback = DexFallback(())
    with pytest.raises(FallbackUnavailable, match="usable party and Pokedex"):
        fallback.from_snapshot(SimpleNamespace(usable=True, pokedex=None, party=()))


@pytest.mark.parametrize("body", [None, [], "dex", {"unknown": "dex"}, {"fallback": True},
                                  {"fallback": "other"}])
def test_invalid_objective_config_rejected_in_both_paths(tmp_path, body):
    import yaml
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"objectives": body}))
    with pytest.raises(AppError):
        config_from_mapping({"objectives": body})
    with pytest.raises(ValueError):
        load_user_config(path, environ={})


@pytest.mark.parametrize("modern", [False, True])
def test_fallback_file_config_reaches_session(tmp_path, monkeypatch, modern):
    from pokegent.games.gen3.firered.rom import wild
    path = tmp_path / "config.yaml"
    path.write_text(("decider: first\n" if modern else "") +
                    "objectives:\n  fallback: dex\nrun:\n  on_objective_done: continue\n")
    raw = load_config_file(path)
    cfg = config_from_mapping({key: value for key, value in raw.items() if key != "hybrid"})
    assert cfg.dex_fallback and cfg.on_objective_done == "continue"
    user = to_legacy_config(load_user_config(path, environ={}))
    assert user["objectives"] == {"fallback": "dex"}
    table = (Encounter("TestA", "land", 1, .5, 2, 3),)
    monkeypatch.setattr(wild, "load_wild_table", lambda: table)
    session = make_objective_session(tmp_path / "objective.json", dex_fallback=cfg.dex_fallback)
    assert session.fallback.table == table


def test_fallback_off_never_loads_rom_cache(tmp_path, monkeypatch):
    from pokegent.games.gen3.firered.rom import wild
    monkeypatch.setattr(wild, "load_wild_table", lambda: pytest.fail("off must not load data"))
    assert make_objective_session(tmp_path / "objective.json").fallback is None


def test_missing_private_data_is_a_redacted_setup_error(tmp_path, monkeypatch):
    from pokegent.games.gen3.firered.rom import wild
    def missing():
        raise FileNotFoundError("synthetic private path must not be printed")
    monkeypatch.setattr(wild, "load_wild_table", missing)
    with pytest.raises(AppError, match="requires valid private ROM encounter data") as error:
        make_objective_session(ObjectiveStore(tmp_path / "objective.json"), dex_fallback=True)
    assert error.value.exit_code == 4
    assert "private path" not in str(error.value)
def test_expiry_remains_authoritative_after_missing_file_warning(tmp_path):
    # Actual store/session, harmless synthetic fallback; no game data.
    from types import SimpleNamespace
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    store = ObjectiveStore(tmp_path / 'objective.json')
    assert store.propose(dict(objective_id='expired', revision=1, kind='hold',
                              expires_at=expiry.isoformat())).accepted
    store.clock = lambda: expiry + timedelta(seconds=1)
    fallback = SimpleNamespace(from_snapshot=lambda _: dict(objective_id='fallback', kind='hold'))
    session = ObjectiveSession(store, fallback=fallback)
    session.refresh()
    assert store.last_warning().code == 'expired'
    store.path.unlink()
    store.current()
    assert store.last_warning().code == 'file_missing'
    session._fallback_after_observe(SimpleNamespace())
    assert store.current() is None
    assert 'expired' in session.fallback_error


def test_fork_capture_precondition_is_hp_only_not_heal_completion():
    from pokegent.goals.tracker import evaluate_probe
    from pokegent.goals.validate import ResolvedProbe
    mon = SimpleNamespace(is_egg=False, hp=10, max_hp=10, status=1,
                          moves=(SimpleNamespace(move_id=1, pp=0),), pp_bonuses=0)
    snapshot = SimpleNamespace(usable=True, player=None, pokedex=SimpleNamespace(owned=()),
                               party=(mon,), map=None,
                               bag=(SimpleNamespace(items=(SimpleNamespace(
                                   item_id=4, quantity=10),)),))
    fallback = DexFallback((Encounter("TestA", "land", 1, .5, 2, 3),))
    assert fallback.from_snapshot(snapshot)["objective_id"] == "dex_1"
    assert not evaluate_probe(ResolvedProbe("party_healed"), snapshot)
