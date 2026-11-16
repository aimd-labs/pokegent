"""The PUBLIC surface: exactly what the stream dashboard reads.

``public_telemetry(operator_doc)`` projects the operator document (``telemetry.build_telemetry``)
onto the keys the dashboard's own JS reads. It is built by naming each value, never by deleting
from the operator document: a field added to the operator side stays off the public side until
somebody adds it here on purpose.

Safe to show to a local browser: no instruction or state
text sent to the model, no option ids, no decision ids, no probabilities keyed by option id, no
run id (only its last six characters, which the page shows as "RUN XXXXXX"), no counters, no
events, no errors, no goal ids. What remains is what a viewer of the stream sees anyway: the
party, the map name, the badges, the question and the options of the current decision with their
probabilities, the pick and its fact sentence, the objective line, the highlights.
"""

from __future__ import annotations

from typing import Any

#: the part of the run id the dashboard shows ("RUN ABC123")
SESSION_TAIL = 6


def _party_row(mon: dict[str, Any]) -> dict[str, Any]:
    return {
        "species_id": mon["species_id"],
        "national_dex": mon.get("national_dex"),
        "species": mon["species"],
        "nickname": mon["nickname"],
        "level": mon["level"],
        "hp": mon["hp"],
        "max_hp": mon["max_hp"],
        "status": mon["status"],
        "types": list(mon["types"]),
    }


def _battle(battle: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "in_battle": battle["in_battle"],
        "is_trainer": battle["is_trainer"],
    }
    if not battle["in_battle"]:
        return out
    out["active_party_indexes"] = list(battle.get("active_party_indexes", []))
    mine, foe = battle.get("player"), battle.get("enemy")
    out["player"] = (
        None
        if mine is None
        else {
            "party_index": mine["party_index"],
            "species": mine["species"],
            "nickname": mine["nickname"],
            "moves": [
                {"name": m["name"], "type": m["type"], "power": m["power"]} for m in mine["moves"]
            ],
        }
    )
    out["enemy"] = (
        None
        if foe is None
        else {
            "species_id": foe["species_id"],
            "national_dex": foe.get("national_dex"),
            "species": foe["species"],
            "nickname": foe["nickname"],
            "level": foe["level"],
            "hp": foe["hp"],
            "max_hp": foe["max_hp"],
            "status": foe["status"],
            "types": list(foe["types"]),
        }
    )
    return out


def _state(state: dict[str, Any]) -> dict[str, Any]:
    player = state.get("player") or {}
    play_time = player.get("play_time")
    pokedex = state.get("pokedex") or {}
    out: dict[str, Any] = {
        "badges": list(state.get("badges", [])),
        "party": [_party_row(m) for m in state.get("party", [])],
        "battle": _battle(state.get("battle") or {"in_battle": False, "is_trainer": False}),
        "player": {
            "name": player.get("name"),
            "trainer_id": player.get("trainer_id"),
            "play_time": (
                {unit: play_time.get(unit) for unit in ("hours", "minutes", "seconds")}
                if play_time is not None else None
            ),
            "money": player.get("money"),
        },
        "pokedex": {"seen": pokedex.get("seen"), "caught": pokedex.get("caught")},
    }
    if state.get("map"):
        out["map"] = {"name": state["map"]["name"]}
        out["position"] = {"x": state["position"]["x"], "y": state["position"]["y"]}
    return out


def _decision(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "family_label": d["family_label"],
        "question_short": d["question_short"],
        "choices": [
            {
                "label": c["label"],
                "probability": c["probability"],
                "selected": c["selected"],
                "executed": c["executed"],
            }
            for c in d["choices"]
        ],
        "options_total": d["options_total"],
        "status": d["status"],
        "source_label": d["source_label"],
        "decided_by": d.get("decided_by"),
        "awaiting_agent": d.get("awaiting_agent", False),
        "latency_ms": d["latency_ms"],
        "confidence": d["confidence"],
        "fact": d["fact"],
        "selected_label": d["selected_label"],
        "ts": d["ts"],
        "updated_at": d["updated_at"],
        "repeat": d["repeat"],
        "seen_10m": d["seen_10m"],
        "recent": [
            {
                "owner": r["owner"],
                "family_label": r["family_label"],
                "pick_label": r["pick_label"],
                "result_label": r["result_label"],
                "repeat": r["repeat"],
                "ts": r["ts"],
            }
            for r in d["recent"]
        ],
    }


def public_telemetry(op: dict[str, Any]) -> dict[str, Any]:
    """The ``/telemetry`` document of the public surface, from the operator document ``op``."""
    document = {
        "generated_at": op["generated_at"],
        "server": {"emulator_ready": op["server"]["emulator_ready"]},
        "game": {"session_id": str(op["game"]["session_id"])[-SESSION_TAIL:]},
        "run_timing": {
            "in_game_seconds": None,
            "split": None,
            "reference_seconds": None,
            "delta_seconds": None,
            "reference_label": None,
        },
        "run_stats": {
            "battles": op["run_stats"]["battles"],
            "blackouts": op["run_stats"]["blackouts"],
        },
        "state": _state(op["state"]),
        "pokegent": {
            "decision": _decision(op["pokegent"]["decision"]),
            "heading": None,
            "live_turn": None,
        },
        "agent": op["agent"],  # a constant: no agent in an offline run
        "objectives": {
            "display": {
                "current": op["objectives"]["display"]["current"],
                "next": op["objectives"]["display"]["next"],
            }
        },
        "readiness": None,
        "battle_matchup": None,
        "highlights": [
            {"ts": h["ts"], "title": h["title"], "category": h["category"], "text": h["text"]}
            for h in op["highlights"]
        ],
    }
    document["pokejev"] = document["pokegent"]
    return document
