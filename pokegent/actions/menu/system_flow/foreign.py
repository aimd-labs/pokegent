"""The back-out safety net for a foreign menu that nobody owns.

A menu no family builds options for is ``scene.unknown`` to the registry: no input, an
escalation, and
the same screen used to repeat forever (the class-G10 freeze). The PC is the case
this package solves outright (:mod:`.evidence_pc`). For other known menus this module is the generic
mechanism: a table of (screen signature -> catalog scene id -> one B). It is OPT-IN per scene id
(``SystemFlowOptions.foreign_menus``) because scene ownership is unique: a family built later may
claim the same id, and that must stay an explicit decision, not a registration crash.

Only screens whose B is the documented way out belong in the table, each with a real-ROM check
before
it is switched on by default.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions.menu.system_flow.evidence import Verdict, callback_name, confidence_cap
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence

_GENERIC_MENU_TYPES = frozenset(
    {
        "start_menu",
        "start_menu_subprompt",
        "bag",
        "party_menu",
        "summary",
        "pc_storage",
    }
)
_FIELD_CALLBACKS = ("CB2_Overworld", "CB2_OverworldBasic")


@dataclass(frozen=True)
class ForeignMenu:
    scene_id: str
    menu: str  # fact value
    ui_menu_type: str  # UiEvidence.menu_type that proves the menu is up (task AND window)
    label: str
    description: str
    verb: str
    callbacks: tuple[str, ...] = ()
    # Callback2 functions installed by the decomp while B is handing this screen
    # back to its owner.  They are deliberately table data: these frames are a
    # busy part of this screen's lifetime, not a new screen or an unknown scene.
    transition_callbacks: tuple[str, ...] = ()


FOREIGN_MENUS: dict[str, ForeignMenu] = {
    "menu.start": ForeignMenu(
        "menu.start",
        "start_menu",
        "start_menu",
        "Close the START menu",
        "Press B to close the START menu without choosing an entry.",
        "EXIT",
    ),
    "pokedex.list": ForeignMenu(
        "pokedex.list",
        "pokedex",
        "pokedex",
        "Back out of the Pokédex",
        "Press B to return to the field or START menu.",
        "EXIT",
        ("CB2_PokedexScreen",),
        ("fn:0x08102858",),  # pokedex_screen.c return/fade callback in the Rev0 ROM
    ),
    "party.summary": ForeignMenu(
        "party.summary",
        "summary",
        "summary",
        "Back out of the Pokemon summary",
        "Press B to return to the party menu.",
        "EXIT",
        ("CB2_SetUpPSS", "obs_cb2_pokemon_summary_screen"),
        ("fn:0x08122DBC",),  # pokemon_summary_screen.c return-to-party callback
    ),
    "menu.trainer_card": ForeignMenu(
        "menu.trainer_card",
        "trainer_card",
        "trainer_card",
        "Back out of the Trainer Card",
        "Press B to return to the field or START menu.",
        "EXIT",
        ("CB2_TrainerCard",),
    ),
    "menu.options": ForeignMenu(
        "menu.options",
        "options",
        "options",
        "Back out of Options",
        "Press B to return to the field or START menu.",
        "EXIT",
        ("CB2_OptionMenu",),
    ),
}

GENERIC_MENU = ForeignMenu(
    "menu.open",
    "menu",
    "",
    "Close the open menu",
    "Press B to close the open menu without choosing an entry.",
    "EXIT",
)


def classify_generic(snapshot: StateSnapshot, ui: UiEvidence) -> Verdict | None:
    """Claim a supported open menu which no more-specific family claimed (agent #67)."""
    from pokegent.actions.menu.dialog.evidence import party_close_message_waiting

    if party_close_message_waiting(ui):
        return None  # The source-bound message acknowledgement owns input, not generic B.
    cb = callback_name(ui)
    # Menu callbacks legitimately change while a bag, party, summary, PC, or
    # START sub-prompt is active.  The corroborated menu evidence, not the
    # overworld callback name, is the ownership boundary for this safety net.
    if ui.main.in_battle:
        return None
    if ui.menu_type not in _GENERIC_MENU_TYPES:
        return None
    cap = confidence_cap(ui, "main", "tasks", "menu")
    return Verdict(
        "menu.open",
        min(0.85, cap),
        False,
        {"callback": cb, "menu": ui.menu_type, "phase": "open"},
    )


def classify(snapshot: StateSnapshot, ui: UiEvidence, enabled: frozenset[str]) -> Verdict | None:
    cb = callback_name(ui)
    if ui.main.in_battle:
        return None
    cap = confidence_cap(ui, "main", "tasks", "menu")
    for scene_id, entry in FOREIGN_MENUS.items():
        if scene_id == "menu.start" and scene_id not in enabled:
            continue
        if cb in (*entry.callbacks, *entry.transition_callbacks) and (
            cb in entry.transition_callbacks
            or not entry.ui_menu_type
            or ui.menu_type == entry.ui_menu_type
        ):
            return Verdict(
                scene_id,
                min(0.95, cap),
                cb in entry.transition_callbacks,
                {
                    "callback": cb,
                    "phase": "transition" if cb in entry.transition_callbacks else entry.menu,
                    "menu": entry.menu,
                },
            )
    if not enabled or cb not in _FIELD_CALLBACKS:
        return None
    for scene_id in sorted(enabled):
        entry = FOREIGN_MENUS.get(scene_id)
        if entry is not None and ui.menu_type == entry.ui_menu_type:
            return Verdict(
                scene_id,
                min(0.9, cap),
                False,
                {"callback": cb, "phase": entry.menu, "menu": entry.menu},
            )
    return None
