"""Observed-time milestone comparisons over the bounded read-only ledger API."""

from __future__ import annotations

import sqlite3

from pokegent.ledger_queries import query


def _clock_decreases(db: sqlite3.Connection) -> set[str]:
    rows = query(
        db,
        "WITH ordered AS (SELECT r.save_id,c.game_seconds,LAG(c.game_seconds) OVER "
        "(PARTITION BY r.save_id ORDER BY c.observed_at,c.event_id) previous "
        "FROM observation_clocks c JOIN runs r ON r.id=c.segment_id "
        "WHERE c.game_seconds IS NOT NULL) "
        "SELECT DISTINCT save_id FROM ordered "
        "WHERE previous IS NOT NULL AND game_seconds<previous",
    )
    return {row["save_id"] for row in rows}


def splits(db: sqlite3.Connection, save_id: str) -> dict[str, object]:
    """Return first-observed elapsed milestones and minima from compatible attempts."""
    selected_rows = query(
        db,
        "SELECT id,mode,rom_profile,comparison_group,game_origin_seconds FROM saves WHERE id=?",
        (save_id,),
        max_rows=1,
    )
    if not selected_rows:
        raise KeyError(save_id)
    selected = selected_rows[0]
    if any(
        selected[k] is None
        for k in ("mode", "rom_profile", "comparison_group", "game_origin_seconds")
    ):
        return {
            "available": False,
            "reason": "selected save lacks known mode, profile, comparison group, or game origin",
            "milestones": [],
        }
    decreased = _clock_decreases(db)
    if save_id in decreased:
        return {
            "available": False,
            "reason": "selected save has a chronological game-clock decrease; choose a new attempt",
            "milestones": [],
        }
    compatible = query(
        db,
        "SELECT id FROM saves WHERE id<>? AND mode=? AND rom_profile=? AND comparison_group=? "
        "AND game_origin_seconds IS NOT NULL",
        (save_id, selected["mode"], selected["rom_profile"], selected["comparison_group"]),
    )
    eligible = [row["id"] for row in compatible if row["id"] not in decreased]
    observations = query(
        db,
        "SELECT m.kind,m.value,c.game_seconds FROM milestones m "
        "JOIN runs r ON r.id=m.segment_id AND r.save_id=m.save_id "
        "LEFT JOIN observation_clocks c ON c.segment_id=m.segment_id "
        "AND c.event_id=m.source_event_id "
        "WHERE m.save_id=? "
        "AND m.observation IN ('achievement','possession') "
        "ORDER BY c.game_seconds,m.id",
        (save_id,),
    )
    first = {}
    for row in observations:
        key = (row["kind"], row["value"])
        if key not in first:
            first[key] = {"elapsed": None, "reason": "milestone has no observation clock receipt"}
        clock = row["game_seconds"]
        if clock is not None and clock < selected["game_origin_seconds"]:
            first[key]["reason"] = "observation clock predates the configured game origin"
        elif clock is not None and first[key]["elapsed"] is None:
            first[key] = {
                "elapsed": clock - selected["game_origin_seconds"],
                "reason": None,
            }
    references = {}
    if eligible:
        marks = ",".join("?" for _ in eligible)
        ref_rows = query(
            db,
            "SELECT m.kind,m.value,MIN(c.game_seconds-s.game_origin_seconds) elapsed "
            "FROM milestones m JOIN runs r ON r.id=m.segment_id AND r.save_id=m.save_id "
            "JOIN saves s ON s.id=m.save_id "
            "JOIN observation_clocks c ON c.segment_id=m.segment_id "
            "AND c.event_id=m.source_event_id "
            f"WHERE m.save_id IN ({marks}) AND c.game_seconds IS NOT NULL "
            "AND m.observation IN ('achievement','possession') "
            "AND c.game_seconds>=s.game_origin_seconds GROUP BY m.kind,m.value",
            eligible,
        )
        references = {(r["kind"], r["value"]): r["elapsed"] for r in ref_rows}
    result = []
    for (kind, value), observed in sorted(first.items()):
        reference = references.get((kind, value))
        result.append(
            {
                "kind": kind,
                "value": value,
                "observed_elapsed_seconds": observed["elapsed"],
                "observation_reason": observed["reason"],
                "reference_elapsed_seconds": reference,
                "delta_seconds": None
                if reference is None or observed["elapsed"] is None
                else observed["elapsed"] - reference,
                "reference_reason": None
                if reference is not None
                else "no compatible observed reference",
            }
        )
    return {"available": True, "reason": None, "milestones": result}
