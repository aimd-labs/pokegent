"""Facts rendering for this family and the run-level ``game_clear`` fact.

* :func:`register_renderers` adds ONE renderer for the keys this family writes into ``Option.facts``
  (contract v1.2: whitelist, deterministic, facts not advice). Registration is idempotent.
* ``game_clear`` (``FLAG_SYS_GAME_CLEAR``, 0x82C) is a RUN-LEVEL fact, not a scene: it stays set for
  the rest of the game, in every scene. The family puts it into the context of every frame it
  builds,
  and :class:`GameClearWatch` reports the first snapshot that shows it (see
  the runtime contracts
  for the one-line runner hook; the objective tracker's ``game_clear`` probe already reads the same
  flag from ``snapshot.flags``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pokegent.actions.menu.system_flow.evidence import game_clear
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options import render


def _move_text(m: Any) -> str | None:
    if not isinstance(m, Mapping):
        return None
    name = m.get("name")
    if not isinstance(name, str):
        return None
    detail = []
    if isinstance(m.get("type"), str):
        detail.append(m["type"])
    if isinstance(m.get("power"), int):
        detail.append(f"power {m['power']}" if m["power"] else "no power")
    if isinstance(m.get("accuracy"), int):
        detail.append(f"accuracy {m['accuracy']}" if m["accuracy"] else "never misses")
    max_pp = m.get("max_pp")
    if isinstance(m.get("pp"), int):
        detail.append(f"PP {m['pp']}/{max_pp}" if isinstance(max_pp, int) else f"PP {m['pp']}")
    elif isinstance(max_pp, int):
        detail.append(f"PP {max_pp}")
    return name + (f" ({', '.join(detail)})" if detail else "")


def render_system_flow(f: Mapping[str, Any]) -> Sequence[str]:
    out: list[str] = []
    ev = f.get("evolution")
    if isinstance(ev, Mapping):
        src, dst = ev.get("from"), ev.get("to")
        if isinstance(src, str) and isinstance(dst, str):
            out.append(f"{src} would evolve into {dst}")
        if ev.get("can_cancel") is True:
            out.append("the evolution can still be cancelled in this window")
        elif ev.get("can_cancel") is False:
            out.append("this evolution cannot be cancelled")
    if f.get("default") is True:
        out.append("the default answer")
    forgets, learns = _move_text(f.get("forgets")), _move_text(f.get("learns"))
    if forgets and learns:
        out.append(f"forgets {forgets} to learn {learns}")
    elif forgets:
        out.append(f"forgets {forgets}")
    new = _move_text(f.get("new_move"))
    if new:
        out.append(f"new move {new}")
    elif f.get("new_move_unknown") is True:
        out.append("the new move is not readable in this screen")
    known = f.get("known_moves")
    if isinstance(known, (list, tuple)):
        texts = [t for t in (_move_text(m) for m in known) if t]
        if texts:
            out.append("known moves: " + "; ".join(texts))
    if f.get("new_game_forbidden") is True and f.get("new_game_mode") is not True:
        out.append("NEW GAME is never chosen (it would start over)")
    if f.get("owner") == "none" and isinstance(f.get("menu"), str):
        out.append(f"the {render._word(f['menu'])} has no owning plan; backing out changes nothing")
    if f.get("game_clear") is True:
        out.append("the game is already cleared")
    return out


def register_renderers() -> None:
    render.register_fact_renderer("system_flow", render_system_flow, replace=True)


@dataclass
class GameClearWatch:
    """Remembers whether the clear flag was seen; ``observe`` is True exactly once, at the first
    usable snapshot that shows it. Pure state, no event system: the caller decides what to emit
    (``EventType.PROGRESS_GAME_CLEARED`` exists and nothing emits it today)."""

    seen: bool = False
    first_snapshot_seq: int | None = None

    def observe(self, snapshot: StateSnapshot) -> bool:
        if self.seen or game_clear(snapshot) is not True:
            return False
        self.seen = True
        self.first_snapshot_seq = snapshot.snapshot_seq
        return True
