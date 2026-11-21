"""Synthetic contract tests for explicit comparable observed-time splits."""

import sqlite3
from contextlib import closing

import pytest

from pokegent import ledger
from pokegent.ledger_observations import ingest_observation
from pokegent.ledger_queries import open_readonly
from pokegent.ledger_splits import splits


def add_save(db, name, *, group="route-a", origin=100, mode="casual", profile="fr"):
    db.execute(
        "INSERT INTO saves(id,mode,rom_profile,comparison_group,game_origin_seconds,source_ref) "
        "VALUES(?,?,?,?,?,'synthetic')",
        (name, mode, profile, group, origin),
    )


def add_sample(db, save_id, seg, event, observed, clock, milestone=None):
    db.execute(
        "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,'synthetic')",
        (seg, seg, save_id),
    )
    source = "src-" + event
    receipt = "ram-observation:" + event
    db.execute(
        "INSERT INTO ingest_sources(id,segment_id,source_ref,generation) VALUES(?,?,?,0)",
        (source, seg, source),
    )
    db.execute(
        "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset) "
        "VALUES(?,?,?,?,0)",
        (seg, receipt, source, 1),
    )
    db.execute("INSERT INTO observation_clocks VALUES(?,?,?,?)", (seg, receipt, observed, clock))
    if milestone:
        kind, value = milestone
        db.execute(
            "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,game_seconds,"
            "source_ref,source_event_id) "
            "VALUES(?,?,?,?,?,'possession',?,'synthetic',?)",
            ("m-" + event, seg, save_id, kind, value, clock, event),
        )


@pytest.fixture
def db(tmp_path):
    with closing(ledger.open_ledger(tmp_path / "ledger.db")) as conn:
        yield conn


def test_v2_migration_preserves_existing_save(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    migrations, version = ledger._MIGRATIONS, ledger.SCHEMA_VERSION
    monkeypatch.setattr(ledger, "_MIGRATIONS", migrations[:2])
    monkeypatch.setattr(ledger, "SCHEMA_VERSION", 2)
    with closing(ledger.open_ledger(path)) as old:
        with old:
            old.execute(
                "INSERT INTO saves(id,label,source_ref) "
                "VALUES('old','sentinel','synthetic')"
            )
        assert old.execute("PRAGMA user_version").fetchone()[0] == 2
    monkeypatch.setattr(ledger, "_MIGRATIONS", migrations)
    monkeypatch.setattr(ledger, "SCHEMA_VERSION", version)
    with closing(ledger.open_ledger(path)) as migrated:
        assert migrated.execute(
            "SELECT label,comparison_group,game_origin_seconds FROM saves WHERE id='old'"
        ).fetchone() == ("sentinel", None, None)
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == version


def test_configuration_requires_known_identity_and_is_immutable(db):
    with db:
        db.execute("INSERT INTO saves(id,source_ref) VALUES('unknown','synthetic')")
        add_save(db, "s")
    with pytest.raises(ValueError, match="mode and ROM"):
        db.execute("BEGIN")
        try:
            ledger.configure_comparison(db, "unknown", comparison_group="x", game_origin_seconds=0)
        finally:
            db.rollback()
    db.execute("BEGIN")
    ledger.configure_comparison(db, "s", comparison_group="route-a", game_origin_seconds=100)
    db.commit()
    with pytest.raises(ValueError, match="immutable"):
        db.execute("BEGIN")
        try:
            ledger.configure_comparison(
                db, "s", comparison_group="route-b", game_origin_seconds=100
            )
        finally:
            db.rollback()
    for bad in (True, -1, 3_600_000):
        with pytest.raises(ValueError):
            db.execute("BEGIN")
            try:
                ledger.configure_comparison(
                    db, "s", comparison_group="route-a", game_origin_seconds=bad
                )
            finally:
                db.rollback()


@pytest.mark.parametrize("profile", ["", "   "])
def test_empty_profile_is_unknown_for_configuration_and_splits(db, profile):
    with db:
        add_save(db, "blank-profile", profile=profile)
    with pytest.raises(ValueError, match="ROM profile"):
        db.execute("BEGIN")
        try:
            ledger.configure_comparison(
                db, "blank-profile", comparison_group="route-a", game_origin_seconds=0
            )
        finally:
            db.rollback()
    assert splits(db, "blank-profile")["available"] is False


def test_origin_matching_reference_and_negative_delta_readonly(db, tmp_path):
    with db:
        add_save(db, "current")
        add_save(db, "ref", origin=500)
        add_sample(db, "current", "c", "ec", "2026-01-01T00:00:00Z", 160, ("badge", "1"))
        add_sample(db, "ref", "r", "er", "2026-01-02T00:00:00Z", 550, ("badge", "1"))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(open_readonly(path)) as ro:
        out = splits(ro, "current")
        assert out["milestones"] == [
            {
                "kind": "badge",
                "value": "1",
                "observed_elapsed_seconds": 60,
                "observation_reason": None,
                "reference_elapsed_seconds": 50,
                "delta_seconds": 10,
                "reference_reason": None,
            }
        ]
        out = splits(ro, "ref")
        assert out["milestones"][0]["delta_seconds"] == -10
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("UPDATE saves SET label='x'")


def test_real_observation_writer_receipts_produce_split_delta(db):
    with db:
        add_save(db, "current", origin=0)
        add_save(db, "reference", origin=0)
        for save_id, segment, observation_id, at, seconds in (
            ("current", "current-seg", "current-observation", "2026-01-01T00:00:00Z", 100),
            ("reference", "reference-seg", "reference-observation", "2026-01-02T00:00:00Z", 120),
        ):
            db.execute(
                "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,'synthetic')",
                (segment, segment, save_id),
            )
            ingest_observation(
                db,
                save_id=save_id,
                segment_id=segment,
                observation_id=observation_id,
                observed_at=at,
                source_ref="synthetic",
                payload={
                    "snapshot_seq": 1,
                    "rom_profile": "fr",
                    "game_seconds": seconds,
                    "milestones": [["badge", "1", "possession"]],
                },
            )
    rows = db.execute(
        "SELECT m.source_event_id,c.event_id FROM milestones m "
        "JOIN observation_clocks c ON c.segment_id=m.segment_id "
        "AND c.event_id='ram-observation:' || m.source_event_id ORDER BY m.save_id"
    ).fetchall()
    assert rows == [
        ("current-observation", "ram-observation:current-observation"),
        ("reference-observation", "ram-observation:reference-observation"),
    ]
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(open_readonly(path)) as ro:
        result = splits(ro, "current")["milestones"]
    assert result == [
        {
            "kind": "badge",
            "value": "1",
            "observed_elapsed_seconds": 100,
            "observation_reason": None,
            "reference_elapsed_seconds": 120,
            "delta_seconds": -20,
            "reference_reason": None,
        }
    ]


def test_unavailable_unknown_and_clock_rollback(db):
    with db:
        add_save(db, "current")
        add_sample(db, "current", "c1", "e1", "2026-01-01T00:00:00Z", 130, ("badge", "1"))
        add_sample(db, "current", "c2", "e2", "2026-01-01T00:01:00Z", 120)
    assert splits(db, "current")["available"] is False
    with pytest.raises(KeyError):
        splits(db, "missing")


def test_clock_only_rollback_and_out_of_order_backfill(db):
    with db:
        add_save(db, "s")
        add_sample(db, "s", "a", "first", "2026-01-01T00:01:00Z", 200)
        add_sample(db, "s", "b", "later", "2026-01-01T00:02:00Z", 150)
    assert splits(db, "s")["available"] is False


def test_incompatible_references_and_null_clock_are_not_used(db):
    with db:
        add_save(db, "s")
        add_save(db, "other", group="other")
        add_sample(db, "s", "s1", "es", "2026-01-01T00:00:00Z", 130, ("badge", "1"))
        add_sample(db, "other", "o1", "eo", "2026-01-02T00:00:00Z", 120, ("badge", "1"))
        add_sample(db, "s", "s2", "null", "2026-01-03T00:00:00Z", None)
    row = splits(db, "s")["milestones"][0]
    assert row["observed_elapsed_seconds"] == 30
    assert row["reference_elapsed_seconds"] is None
    assert row["delta_seconds"] is None


@pytest.mark.parametrize(
    "override",
    [
        {"mode": "speedrun"},
        {"profile": "emerald"},
        {"group": "different"},
        {"group": None},
        {"origin": None},
        {"mode": None},
        {"profile": None},
        {"profile": ""},
        {"profile": "   "},
    ],
)
def test_references_require_all_known_matching_metadata(db, override):
    with db:
        add_save(db, "s")
        add_save(db, "other", **override)
        add_sample(db, "s", "s1", "es", "2026-01-01T00:00:00Z", 130, ("badge", "1"))
        add_sample(db, "other", "o1", "eo", "2026-01-02T00:00:00Z", 120, ("badge", "1"))
    row = splits(db, "s")["milestones"][0]
    assert row["reference_elapsed_seconds"] is None


def test_selected_save_is_not_its_own_reference_and_milestone_needs_clock(db):
    with db:
        add_save(db, "s")
        add_sample(db, "s", "s1", "clock", "2026-01-01T00:00:00Z", 130)
        db.execute(
            "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,game_seconds,"
            "source_ref,source_event_id) "
            "VALUES('legacy','s1','s','badge','2','possession',125,'synthetic','missing-receipt')"
        )
    result = splits(db, "s")
    assert result["milestones"][0]["observed_elapsed_seconds"] is None
    assert result["milestones"][0]["observation_reason"] == (
        "milestone has no observation clock receipt"
    )


def test_milestone_before_origin_is_not_negative_elapsed(db):
    with db:
        add_save(db, "s", origin=150)
        add_sample(db, "s", "s1", "early", "2026-01-01T00:00:00Z", 140, ("badge", "1"))
    split = splits(db, "s")["milestones"][0]
    assert split["observed_elapsed_seconds"] is None
    assert split["observation_reason"] == "observation clock predates the configured game origin"
