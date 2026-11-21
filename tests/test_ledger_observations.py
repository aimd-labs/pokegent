"""Synthetic RAM observation and offline ledger admission; no ROM or live data."""

import sqlite3

import pytest

from pokegent.dashboard.game import observation_block, state_block
from pokegent.dashboard.names import NameBook
from pokegent.dashboard.public import public_telemetry
from pokegent.games.gen3.firered.reader.schemas import (
    BagItem,
    BagPocket,
    FlagSet,
    Health,
    Player,
    PlayTime,
    Pokedex,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.ledger import open_ledger
from pokegent.ledger_observations import ingest_observation, milestone_payload


def snap(*, valid=True, ready=True):
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=1,
        health=Health(valid=valid),
        player=(
            Player(
                name="Trainer" if ready else "",
                gender="male",
                tid=1,
                sid=2,
                map_group=3,
                map_num=4,
                map_layout_id=0,
                x=0,
                y=0,
                money=0,
                coins=0,
                badges=(1, 3),
                play_time=PlayTime(hours=2, minutes=3, seconds=4),
            )
            if valid
            else None
        ),
        bag=(
            BagPocket(pocket="key_items", capacity=2, items=(BagItem(item_id=7, quantity=1),)),
            BagPocket(
                pocket="tms_hms",
                capacity=2,
                items=(BagItem(item_id=339, quantity=1), BagItem(item_id=346, quantity=0)),
            ),
        ),
        flags=FlagSet(set_ids=(12, 99)),
        vars=VarTable(),
        pokedex=Pokedex(owned=(1,), seen=(1, 2)),
    )


def test_knownness_and_compact_projection(monkeypatch):
    monkeypatch.setattr("pokegent.dashboard.game.map_names.map_name", lambda *_: None)
    block = observation_block(snap())
    assert state_block(snap(), NameBook())["observation"] == block
    assert block == {
        "snapshot_seq": 1,
        "rom_profile": "synthetic",
        "game_seconds": 7384,
        "badges": [1, 3],
        "key_items": [7],
        "hms": [339],
        "map": "3:4",
        "pokedex": {"seen": 2, "owned": 1},
        "flags": [12, 99],
    }
    invalid = snap().model_copy(
        update={
            "health": Health(
                valid=False,
                read_errors=(ReadError(subsystem="test", code="invalid", message="synthetic"),),
            ),
            "player": None,
            "flags": None,
            "vars": None,
            "bag": (),
            "pokedex": None,
        }
    )
    assert observation_block(invalid)["game_seconds"] is None
    assert observation_block(snap(ready=False))["badges"] is None
    absent_pocket = snap().model_copy(update={"bag": ()})
    assert observation_block(absent_pocket)["key_items"] is None
    missing_flags = snap().model_copy(update={"flags": None})
    payload = milestone_payload(observation_block(missing_flags), (12, 777))
    assert not [r for r in payload["milestones"] if r[0] == "story_flag"]
    selected = milestone_payload(observation_block(snap()), (12, 777))
    assert [r for r in selected["milestones"] if r[0] == "story_flag"] == [
        ["story_flag", "12", "possession"]
    ]


def test_operator_private_fields_stay_out_of_public_projection():
    operator = {
        "generated_at": "now",
        "server": {"emulator_ready": False},
        "game": {"session_id": "synthetic"},
        "run_stats": {"battles": 0, "blackouts": 0},
        "state": {
            "player": {},
            "pokedex": {},
            "party": [],
            "battle": {"in_battle": False, "is_trainer": False},
            "observation": {"flags": [12], "key_items": [7]},
        },
        "pokegent": {"decision": None},
        "agent": None,
        "objectives": {"display": {"current": None, "next": None}},
        "highlights": [],
    }
    assert "observation" not in public_telemetry(operator)["state"]


def test_ingest_idempotence_conflict_rollback_and_attribution(tmp_path):
    db = open_ledger(tmp_path / "ledger.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('seg','run','s','synthetic')")
    db.commit()
    payload = milestone_payload(observation_block(snap()), (12,))
    with db:
        db.execute("BEGIN")
        inserted = ingest_observation(
            db,
            save_id="s",
            segment_id="seg",
            observation_id="o1",
            observed_at="2026-11-21T10:00:00Z",
            source_ref="synthetic",
            payload=payload,
        )
    assert inserted == len(payload["milestones"])
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="o1",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=payload,
            )
            == 0
        )
    changed = {**payload, "milestones": payload["milestones"][:-1]}
    with pytest.raises(ValueError, match="reused"):
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="o1",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=changed,
            )
    db.execute(
        "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('seg2','run','s','synthetic')"
    )
    lower = {
        "snapshot_seq": 2,
        "rom_profile": "synthetic",
        "game_seconds": 1,
        "milestones": [["badge", "2", "possession"]],
    }
    with db:
        ingest_observation(
            db,
            save_id="s",
            segment_id="seg2",
            observation_id="o2",
            observed_at="2026-11-21T10:01:00Z",
            source_ref="synthetic",
            payload=lower,
        )
    assert (
        db.execute("SELECT observation FROM milestones WHERE source_event_id='o2'").fetchone()[0]
        == "rollback"
    )
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="o1",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=payload,
            )
            == 0
        )
    with pytest.raises(ValueError, match="attribution"):
        db.execute("BEGIN")
        ingest_observation(
            db,
            save_id="other",
            segment_id="seg",
            observation_id="o3",
            observed_at="2026-11-21T10:02:00Z",
            source_ref="synthetic",
            payload=payload,
        )
    db.close()


def test_payload_rejects_coercions_and_empty_receipts_detect_content_conflict(tmp_path):
    known = observation_block(snap())
    with pytest.raises(ValueError, match="game_seconds"):
        milestone_payload({**known, "game_seconds": True})
    with pytest.raises(ValueError, match="badge"):
        milestone_payload({**known, "badges": [True]})

    db = open_ledger(tmp_path / "empty.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('seg','run','s','synthetic')")
    db.commit()
    empty = {"snapshot_seq": 0, "rom_profile": "synthetic", "game_seconds": None, "milestones": []}
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="empty",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=empty,
            )
            == 0
        )
    assert db.execute("SELECT seq FROM ingested_events").fetchone()[0] == 1
    assert db.execute("SELECT rom_profile FROM saves WHERE id='s'").fetchone()[0] == "synthetic"
    changed = {**empty, "game_seconds": 1}
    with pytest.raises(ValueError, match="reused"):
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="empty",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=changed,
            )
    with pytest.raises(ValueError, match="ISO-8601"):
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="bad-time",
                observed_at="not-a-time",
                source_ref="synthetic",
                payload=empty,
            )
    with pytest.raises(ValueError, match="ROM profile"):
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id="seg",
                observation_id="profile-conflict",
                observed_at="2026-11-21T10:03:00Z",
                source_ref="synthetic",
                payload={**empty, "rom_profile": "other"},
            )
    db.close()


def test_clock_rollback_is_chronological_across_segments(tmp_path):
    db = open_ledger(tmp_path / "clock.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    for segment in ("a", "b"):
        db.execute(
            "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,?)",
            (segment, "run", "s", "synthetic"),
        )
    db.commit()

    def record(segment, identity, seq, seconds, when, badge):
        payload = {
            "snapshot_seq": seq,
            "rom_profile": "synthetic",
            "game_seconds": seconds,
            "milestones": [["badge", str(badge), "possession"]],
        }
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id=segment,
                observation_id=identity,
                observed_at=when,
                source_ref="synthetic",
                payload=payload,
            )

    record("a", "clock1", 1, 100, "2026-11-21T10:00:00Z", 1)
    record("b", "clock2", 1, 50, "2026-11-21T10:01:00Z", 2)
    record("a", "clock3", 2, 60, "2026-11-21T10:02:00Z", 3)
    record("a", "backfill", 3, 5, "2026-11-21T09:59:00Z", 4)
    assert (
        db.execute("SELECT observation FROM milestones WHERE source_event_id='clock2'").fetchone()[
            0
        ]
        == "rollback"
    )
    assert (
        db.execute("SELECT observation FROM milestones WHERE source_event_id='clock3'").fetchone()[
            0
        ]
        == "possession"
    )
    assert (
        db.execute(
            "SELECT observation FROM milestones WHERE source_event_id='backfill'"
        ).fetchone()[0]
        == "possession"
    )
    db.close()


def test_map_first_visit_is_first_ledger_observation_per_save(tmp_path):
    db = open_ledger(tmp_path / "map.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('a','run','s','synthetic')")
    db.commit()
    payload = {
        "snapshot_seq": 1,
        "rom_profile": "synthetic",
        "game_seconds": 10,
        "milestones": [["map_first_visit", "3:4", "possession"]],
    }
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="a",
                observation_id="map1",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=payload,
            )
            == 1
        )
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('b','run','s','synthetic')")
    db.commit()
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="b",
                observation_id="map2",
                observed_at="2026-11-21T10:01:00Z",
                source_ref="synthetic",
                payload=payload,
            )
            == 0
        )
    backfill = {**payload, "snapshot_seq": 2}
    with db:
        db.execute("BEGIN")
        assert (
            ingest_observation(
                db,
                save_id="s",
                segment_id="a",
                observation_id="map-backfill",
                observed_at="2026-11-21T09:00:00Z",
                source_ref="synthetic",
                payload=backfill,
            )
            == 1
        )
    assert (
        db.execute("SELECT COUNT(*) FROM milestones WHERE kind='map_first_visit'").fetchone()[0]
        == 2
    )
    assert db.execute(
        "SELECT wall_at FROM milestones WHERE kind='map_first_visit' ORDER BY wall_at"
    ).fetchall() == [("2026-11-21T09:00:00.000000Z",), ("2026-11-21T10:00:00.000000Z",)]
    db.close()


def test_map_only_samples_keep_clocks_after_map_dedup(tmp_path):
    db = open_ledger(tmp_path / "map-clock.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('a','run','s','synthetic')")
    db.commit()

    def record(identity, seq, seconds, map_id):
        payload = {
            "snapshot_seq": seq,
            "rom_profile": "synthetic",
            "game_seconds": seconds,
            "milestones": [] if map_id is None else [["map_first_visit", map_id, "possession"]],
        }
        with db:
            db.execute("BEGIN")
            return ingest_observation(
                db,
                save_id="s",
                segment_id="a",
                observation_id=identity,
                observed_at=f"2026-11-21T10:00:{seq:02d}Z",
                source_ref="synthetic",
                payload=payload,
            )

    assert record("m1", 1, 100, "1:1") == 1
    assert record("clock-only", 2, 125, None) == 0
    assert record("m1-repeat", 3, 200, "1:1") == 0
    assert record("m2", 4, 150, "1:2") == 1
    assert db.execute(
        "SELECT game_seconds FROM observation_clocks ORDER BY observed_at"
    ).fetchall() == [(100,), (125,), (200,), (150,)]
    assert (
        db.execute("SELECT COUNT(*) FROM milestones WHERE kind='map_first_visit'").fetchone()[0]
        == 2
    )
    assert (
        db.execute("SELECT observation FROM milestones WHERE value='1:2'").fetchone()[0]
        == "rollback"
    )
    db.close()


def test_v1_upgrade_adds_typed_composite_clock_table(tmp_path, monkeypatch):
    from contextlib import closing

    from pokegent import ledger

    path = tmp_path / "v1.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_MIGRATIONS", ledger._MIGRATIONS[:1])
        patch.setattr(ledger, "SCHEMA_VERSION", len(ledger._MIGRATIONS))
        with closing(ledger.open_ledger(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with closing(ledger.open_ledger(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        foreign_keys = db.execute("PRAGMA foreign_key_list(observation_clocks)").fetchall()
        assert {row[2] for row in foreign_keys} == {"ingested_events"}
        db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
        db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('a','r','s','x')")
        db.execute(
            "INSERT INTO ingest_sources(id,segment_id,source_ref,generation) "
            "VALUES('src','a','x',0)"
        )
        db.execute(
            "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset) "
            "VALUES('a','ram-observation:e','src',1,0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO observation_clocks VALUES('a','missing','2026-01-01T00:00:00Z',1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO observation_clocks "
                "VALUES('a','ram-observation:e','2026-01-01T00:00:00Z',1.5)"
            )
        db.rollback()


def test_receipt_clock_and_milestones_share_caller_transaction(tmp_path):
    db = open_ledger(tmp_path / "atomic-observation.sqlite")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('s','synthetic')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('a','r','s','x')")
    db.execute(
        "CREATE TRIGGER reject_milestone BEFORE INSERT ON milestones "
        "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    db.commit()
    payload = {
        "snapshot_seq": 1,
        "rom_profile": "synthetic",
        "game_seconds": 100,
        "milestones": [["map_first_visit", "1:1", "possession"]],
    }
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        with db:
            db.execute("BEGIN")
            ingest_observation(
                db,
                save_id="s",
                segment_id="a",
                observation_id="atomic",
                observed_at="2026-11-21T10:00:00Z",
                source_ref="synthetic",
                payload=payload,
            )
    assert db.execute("SELECT COUNT(*) FROM ingested_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM observation_clocks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM ingest_sources").fetchone()[0] == 0
    db.close()
