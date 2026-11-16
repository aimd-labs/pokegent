"""``GET /telemetry``: the viewer's JSON document, a superset of the pre-fork stream dashboard's.

:func:`build_telemetry` is a pure function of :meth:`ViewerState.view` (plain data). The key names
and shapes of the pre-fork dashboard are kept (``state.party[*]``, ``state.battle``,
``pokegent.decision`` with ``choices`` / ``options`` / ``probabilities`` / ``selected`` /
``fact_sentence``, ``agent``, ``objectives.display``, ``highlights``, ``server.emulator_ready``);
whatever this runner does not know is ``null`` or an empty list, never invented. The extras
(``viewer``, ``scene``, ``pokegent.decisions``, ``recent_actions``, ``problems``) are ours.

Never in here: executor references and arguments, fingerprints and state hashes, environment,
secrets, paths, error message texts. Each value is copied from a whitelist
(``docs/addons.md``, table "Telemetry fields").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA = "pokegent-viewer/1"
LEGACY_SCHEMA = "pokejev-viewer/1"
MAX_CHOICES = 12
#: counters of the runner that are plain integers worth showing
_COUNTERS = (
    "iterations",
    "actions_applied",
    "actions_verified",
    "actions_rejected",
    "actions_interrupted",
    "actions_failed",
    "forced",
    "decided",
    "invalid_choices",
    "busy_waits",
    "unknown_scenes",
    "unsupported_scenes",
    "escalations",
    "stuck",
)
_TTL = {"state": 2.0, "decision": 6.0, "highlights": 60.0, "run": 15.0}


def _choices(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Option rows for the card: most probable first (frame order for equal/unknown), the pick
    always included, at most ``MAX_CHOICES``."""
    rows = list(enumerate(record["options"]))
    rows.sort(key=lambda item: (-(item[1]["probability"] or 0.0), item[0]))
    keep = [r for _, r in rows[:MAX_CHOICES]]
    pick = next((r for _, r in rows if r["selected"]), None)
    if pick is not None and pick not in keep:
        keep[-1] = pick
    done = record["status"] == "done"
    return [
        {
            "key": r["key"],
            "label": r["label"],
            "text": r["text"],
            "probability": r["probability"],
            "selected": r["selected"],
            "executed": bool(r["selected"] and done),
            "legality": r["legality"],
            "risk": r["risk"],
        }
        for r in keep
    ]


def _family_label(record: dict[str, Any]) -> str:
    scene = record["scene"]
    tail = scene.split(".", 1)[1] if "." in scene else ""
    head = record["family_label"]
    return f"{head} · {tail.replace('_', ' ').upper()}" if tail else head


def _status(record: dict[str, Any]) -> str:
    return {
        "thinking": "thinking",
        "executing": "picked",
        "done": "picked",
        "error": "fallback",
        "timeout": "fallback",
        "invalid": "fallback",
    }.get(record["status"], "picked")


def _repeat(decisions: list[dict[str, Any]]) -> int:
    """How many decisions in a row (newest first) chose the same option in the same scene."""
    if not decisions or decisions[0]["selected"] is None:
        return 0
    head = (decisions[0]["scene"], decisions[0]["selected"])
    count = 0
    for d in decisions:
        if (d["scene"], d["selected"]) != head:
            break
        count += 1
    return count


def decision_view(
    record: dict[str, Any],
    newest_first: list[dict[str, Any]],
    picks: list[tuple[float, str, str]] | None = None,
) -> dict[str, Any]:
    """``pokegent.decision``: the old card's fields plus the full record."""
    source = record["source"]
    seen = sum(
        1
        for _, scene, pick in picks or ()
        if (scene, pick) == (record["scene"], record["selected"])
    )
    recent = [
        {
            "owner": "system1" if d["source"] == "system1" else "local",
            "family_label": _family_label(d),
            "pick_label": d["selected_label"],
            "result_label": d["status"],
            "repeat": 1,
            "ts": d["ts"],
        }
        for d in newest_first
        if d["id"] != record["id"]
    ][:3]
    return {
        # the pre-fork contract (the runtime contracts "Data audit")
        "family": record["family"],
        "question": record["question"],
        "options": {o["key"]: o["text"] for o in record["options"]},
        "probabilities": record["probabilities"],
        "selected": record["selected"],
        "fact_sentence": record["fact_sentence"],
        "source": source,
        "decided_by": record.get("decided_by"),
        "awaiting_agent": record.get("awaiting_agent", False),
        "latency_ms": record["latency_ms"],
        # b21/b42c card fields
        "family_label": _family_label(record),
        "question_short": record["question_short"],
        "choices": _choices(record),
        "options_total": record["options_total"],
        "selected_label": record["selected_label"],
        "fact": record["fact_sentence"],
        "status": _status(record),
        "source_label": source.upper(),
        "confidence": record["confidence"],
        "ts": record["ts"],
        "updated_at": record["ts"],
        "repeat": 1 if record["status"] == "thinking" else max(1, _repeat(newest_first)),
        # the same pick again and again within 10 minutes is a loop signal, except in a fight, where
        # the same move turn after turn is normal play (the old card does the same)
        "seen_10m": 0 if record["selected"] is None or record["family"] == "battle" else seen,
        "recent": recent,
        # ours
        "id": record["id"],
        "scene": record["scene"],
        "goal": record["goal"],
        "goal_summary": record["goal_summary"],
        "provider": record["provider"],
        "model": record["model"],
        "truncated": record["truncated"],
        "run_status": record["status"],
        "error": record["error"],
        "result": record["result"],
    }


def _agent() -> dict[str, Any]:
    line = "No agent in this run: the objective queue is scripted or absent."
    return {
        "enabled": False,
        "status": "off",
        "current": None,
        "summary": {
            "state": "off",
            "label": "OFF",
            "headline": line,
            "last_consult": None,
            "last_incident": None,
            "updated_at": None,
        },
    }


def _objectives(goal: dict[str, Any] | None) -> dict[str, Any]:
    goal = goal or {}
    return {
        "display": {"current": goal.get("summary"), "next": goal.get("next")},
        "active": (
            None
            if not goal.get("goal_id")
            else {
                "objective_id": goal.get("goal_id"),
                "revision": goal.get("revision"),
                "kind": goal.get("kind"),
                "set_by": goal.get("set_by"),
                "done": goal.get("done"),
            }
        ),
    }


def _freshness(view: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    stamps = {
        "state": view["state_at"],
        "decision": latest["ts"] if latest else None,
        "highlights": view["highlights"][-1]["ts"] if view["highlights"] else None,
        "run": view["now"],
    }
    return {k: {"updated_at": v, "ttl_s": _TTL[k]} for k, v in stamps.items() if v is not None}


def build_telemetry(view: dict[str, Any]) -> dict[str, Any]:
    now = view["now"]
    run = view["run"]
    stats = view["stats"]
    pending = view.get("pending")
    picks = view.get("picks", [])
    newest_first = list(reversed(view["decisions"]))
    latest = pending or (newest_first[0] if newest_first else None)
    counters = {k: v for k, v in run["counters"].items() if k in _COUNTERS}
    wall = round((run["ended_at"] or now) - run["started_at"], 1)
    state = view["state"] or {
        "valid": False,
        "badges": [],
        "party": [],
        "battle": {"in_battle": False, "is_trainer": False},
    }
    latencies = stats["decisions"] - stats["decider_errors"]
    scene = dict(view["scene"])
    if scene and view["scene_since"] is not None:
        scene["since"] = round(view["scene_since"], 3)
    actions_done = counters.get("actions_applied", 0)
    document = {
        "schema": SCHEMA,
        "schema_legacy": LEGACY_SCHEMA,
        "generated_at": now,
        "server": {
            "emulator_ready": view["ready"],
            "uptime_seconds": round(now - run["started_at"], 1),
            "kind": "pokegent viewer",
            "read_only": True,
        },
        "game": {"session_id": run["run_id"], "run_id": run["run_id"]},
        "run": {
            "run_id": run["run_id"],
            "status": run["status"],
            "stop_reason": run["stop_reason"],
            "exit_code": run["exit_code"],
            "decider": run["decider"],
            "step": counters.get("iterations", 0),
            "max_steps": run["max_steps"],
            "realtime": run["realtime"],
            "wall_seconds": wall,
            "emulator_frame": run["emulator_frame"],
            "counters": counters,
        },
        "run_timing": {
            "in_game_seconds": None,
            "split": None,
            "reference_seconds": None,
            "delta_seconds": None,
            "reference_label": None,
            "wall_seconds": wall,
            "emulator_seconds": (
                None if run["emulator_frame"] is None else round(run["emulator_frame"] / 60.0, 1)
            ),
        },
        "run_stats": {
            "battles": stats["battles"],
            "blackouts": stats["blackouts"],
            "decisions": stats["decisions"],
            "forced": stats["forced"],
            "actions": actions_done,
        },
        "scene": scene,
        "state": state,
        "pokegent": {
            "decision": decision_view(latest, newest_first, picks) if latest else None,
            "decisions": [decision_view(d, newest_first, picks) for d in newest_first],
            "heading": None,
            "live_turn": None,
            "metrics": {
                "decisions": stats["decisions"],
                "forced": stats["forced"],
                "errors": stats["decider_errors"],
                "latency_ms_last": stats["latency_ms_last"],
                "latency_ms_mean": (
                    round(stats["latency_ms_total"] / latencies, 1) if latencies > 0 else None
                ),
            },
        },
        "agent": _agent(),
        "objectives": _objectives(view["goal"]),
        "readiness": None,
        "battle_matchup": None,
        "highlights": view["highlights"],
        "recent_actions": list(reversed(view["actions"])),
        "problems": list(reversed(view["problems"])),
        "freshness": _freshness(view, latest),
    }
    document["pokejev"] = document["pokegent"]
    return document


#: keys that change with the clock alone: left out of the ETag so an idle run answers 304
_VOLATILE = (("generated_at",), ("server", "uptime_seconds"), ("freshness",))


def etag_of(doc: dict[str, Any]) -> str:
    trimmed = json.loads(json.dumps(doc, default=str))
    for path in _VOLATILE:
        node = trimmed
        for key in path[:-1]:
            node = node.get(key, {})
        node.pop(path[-1], None)
    trimmed.get("run", {}).pop("wall_seconds", None)
    trimmed.get("run_timing", {}).pop("wall_seconds", None)
    body = json.dumps(trimmed, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
