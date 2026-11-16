"""Overridable defaults and the optional data source of the family (doctrine: code supplies
facts and
defaults, agent steers).

``SystemFlowOptions`` is what ``FamilyConfig.options["system_flow"]`` may carry (or a plain
mapping with
the same keys). Everything has a safe default; a family without options works.

* ``evolution``: ``"allow"`` (default) lists ``allow_evolution`` first and ``cancel_evolution``
second,
  ``"cancel"`` swaps the ranking. Either way both are options with their facts: a decider that sees
  the option list can still choose, and agent changes the default by changing this value. B is never
  pressed outside the one cancel window of the decomp, whatever the value.
* ``tables``: static game data for the facts of a learn-move choice (names, type, power, PP).
Anything
  with ``move(id)`` / ``move_name(id)`` / ``species_name(id)`` / ``type_name(id)`` works
  (``pokegent.games.gen3.firered.rom.rom_tables.GameTables``). ``None`` loads them from
  ``$FIRERED_ROM_PATH`` the
  first time they are needed; if that fails the facts say the names are unknown (fail-open:
  facts are
  an optional addon, the input path never depends on them).
* ``learn_loop_limit``: how often one learn-move question may be asked in one learn session
before the
  option that loops back to the previous question (`keep_trying`) is withdrawn and only the option
  that ends the scene remains (with the reason in its text). The loop cannot be chosen forever;
  which
  way out is taken is still the decider's. ``0`` switches the bound off.
* ``quest_log``: ``"skip"`` (default) builds the options of ``system.quest_log_playback`` here:
a CONDITIONAL
  ``skip_quest_log`` (ONE B, input not verified on this ROM, checked by ``gQuestLogState``
  leaving 2/3) and a
  ``wait_for_phase``; the dialog family keeps the detector and hands the builder over. ``"off"``
  leaves the
  dialog family's UNSUPPORTED diagnostic (the run escalates there).
* ``foreign_menus``: scene ids of the back-out safety net that are switched on (see
  :mod:`.foreign`). Empty by default: a scene id nobody owns today may belong to a family that is
  built later, and ownership is unique (``OwnershipError``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

EVOLUTION_DEFAULTS = ("allow", "cancel")
QUEST_LOG_MODES = ("skip", "off")
GENDERS = ("male", "female")


@dataclass(frozen=True)
class NewGameOptions:
    """Inputs used only by an explicit cold-boot new-game run."""

    enabled: bool = False
    gender: Literal["male", "female"] = "male"

    def __post_init__(self) -> None:
        if self.gender not in GENDERS:
            raise ValueError(f"new_game.gender must be one of {GENDERS}")


@dataclass(frozen=True)
class SystemFlowOptions:
    evolution: Literal["allow", "cancel"] = "allow"
    tables: Any = None
    rom_path: str | None = None
    menus: Any = None
    foreign_menus: tuple[str, ...] = ()
    learn_loop_limit: int = 3
    quest_log: Literal["skip", "off"] = "skip"
    #: frames one wait action may run in the long phases (credits are ~15,000 frames)
    credits_chunk_frames: int = 4000
    wait_chunk_frames: int = 900
    new_game: NewGameOptions = field(default_factory=NewGameOptions)
    _lazy: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.evolution not in EVOLUTION_DEFAULTS:
            raise ValueError(f"evolution must be one of {EVOLUTION_DEFAULTS}")
        if self.quest_log not in QUEST_LOG_MODES:
            raise ValueError(f"quest_log must be one of {QUEST_LOG_MODES}")
        if self.learn_loop_limit < 0:
            raise ValueError("learn_loop_limit must be >= 0")
        if self.credits_chunk_frames < 60 or self.wait_chunk_frames < 30:
            raise ValueError("wait chunks are too small to make progress")

    def game_tables(self) -> Any:
        """The static tables, loaded once and only on demand; ``None`` when they cannot be had."""
        if self.tables is not None:
            return self.tables
        if "tables" not in self._lazy:
            self._lazy["tables"] = _tables_from_path(
                self.rom_path or os.environ.get("FIRERED_ROM_PATH")
            )
        return self._lazy["tables"]


def _tables_from_path(path: str | None) -> Any:
    if not path or not os.path.isfile(path):
        return None
    try:
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables

        return GameTables.from_rom_file(path)
    except Exception:  # an optional addon: no tables means no names, never a stopped run
        return None


def coerce_options(config: Any) -> SystemFlowOptions:
    """``None`` / options / mapping to options. Unknown mapping keys are an error (typos matter)."""
    if config is None:
        return SystemFlowOptions()
    if isinstance(config, SystemFlowOptions):
        return config
    if isinstance(config, Mapping):
        allowed = {
            "evolution",
            "tables",
            "rom_path",
            "menus",
            "foreign_menus",
            "learn_loop_limit",
            "quest_log",
            "credits_chunk_frames",
            "wait_chunk_frames",
            "new_game",
        }
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ValueError(f"unknown system_flow options {unknown}; known: {sorted(allowed)}")
        values = dict(config)
        if "foreign_menus" in values:
            values["foreign_menus"] = tuple(values["foreign_menus"])
        if "new_game" in values:
            values["new_game"] = NewGameOptions(**dict(values["new_game"]))
        return SystemFlowOptions(**values)
    raise TypeError(f"cannot read system_flow options from {type(config).__name__}")
