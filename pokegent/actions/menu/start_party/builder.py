"""MenuModel-backed decider options for START and regular field-party menus."""

from __future__ import annotations

import re
from typing import Any

from pokegent.actions._exec.contracts import (
    ActionContract,
    ExpectedState,
    MenuEntry,
    MenuModel,
)
from pokegent.actions.items import EffectClass, ItemOptionBuilder
from pokegent.actions.menu.start_party import refs as R
from pokegent.actions.menu.start_party.facts import _tables_from_env, party_facts
from pokegent.games.gen3.firered.reader.menu_decoders import (
    decode_party_action,
    decode_party_menu,
    decode_party_reorder,
    decode_start_menu,
)
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft

START_VERBS = {
    "start:pokedex": "POKEDEX",
    "start:pokemon": "POKEMON",
    "start:bag": "BAG",
    "start:player_card": "PLAYER_CARD",
    "start:save": "SAVE",
    "start:options": "OPTIONS",
    "start:exit": "EXIT",
}
ACTION_VERBS = {
    "party_action:summary": "SUMMARY",
    "party_action:switch": "SWITCH",
    "party_action:item": "ITEM",
    "party_action:mail": "MAIL",
    "party_action:cancel": "CANCEL",
    "party_action:give": "GIVE_ITEM",
    "party_action:take": "TAKE_ITEM",
    "party_action:read": "READ_MAIL",
    "party_action:take_mail": "TAKE_MAIL",
}


def decode_owned_menu(snapshot: StateSnapshot) -> MenuModel | None:
    """Decode whichever owned menu is live, without relying on stale persistent structs."""
    start = decode_start_menu(snapshot)
    if start is not None:
        return start
    ui = snapshot.ui
    party = ui.menu.party if ui is not None and ui.valid else None
    if party is None or party.active_hint is not True:
        return None
    task_names = {task.func.name for task in ui.tasks.active if task.func.name is not None}
    if "Task_HandleSelectionMenuInput" in task_names:
        return decode_party_action(snapshot)
    if "Task_HandleChooseMonInput" in task_names:
        return decode_party_menu(snapshot)
    return None


def _model(snapshot: StateSnapshot, scene: str) -> MenuModel | None:
    if scene == R.START:
        return decode_start_menu(snapshot)
    if scene == R.PARTY_LIST:
        return decode_party_menu(snapshot)
    if scene == R.PARTY_REORDER:
        return decode_party_reorder(snapshot)
    if scene in (R.PARTY_ACTION, R.PARTY_HELD_ITEM):
        model = decode_party_action(snapshot)
        return model if isinstance(model, MenuModel) else None
    return None


def _objective(ctx: BuildContext) -> str:
    return " ".join((ctx.objective or "no active objective").split())[:240]


def _facts(ctx: BuildContext, entry: MenuEntry, *, novelty: str = "n/a") -> dict[str, Any]:
    return {
        "menu_choice": True,
        "menu_objective": _objective(ctx),
        "menu_cost": "one menu action",
        "menu_prerequisites": "enabled" if entry.enabled else "disabled",
        "menu_novelty": novelty,
        "menu_outcome": entry.effect or f"select {entry.label}",
        "menu_risk": "none",
        "menu_reversible": True,
        **({"target_screen": dict(entry.target_screen)} if entry.target_screen else {}),
    }


def _fingerprint(scene: str, model: MenuModel, snapshot: StateSnapshot) -> str:
    party_menu = snapshot.ui.menu.party if snapshot.ui and snapshot.ui.menu else None
    return fingerprint(
        scene,
        surface=model.surface,
        entries=tuple(
            (entry.stable_key, entry.label, entry.effect, entry.enabled) for entry in model.entries
        ),
        party=tuple(
            (
                mon.personality,
                mon.slot,
                mon.held_item,
                tuple(move.move_id for move in mon.moves),
            )
            for mon in snapshot.party
        ),
        party_action=getattr(party_menu, "action_name", None),
        party_item=getattr(party_menu, "item_id", None),
    )


def _expected(
    scene: str, entry: MenuEntry, snapshot: StateSnapshot, *, item_id: int | None = None
) -> tuple[ExpectedState, ...]:
    key = entry.stable_key
    if scene == R.START and key == "start:pokemon" and snapshot.party:
        return (
            ExpectedState(
                kind="menu_open_cursor",
                surface=R.PARTY_LIST,
                stable_key=f"party:{snapshot.party[0].personality:08x}",
            ),
        )
    if scene == R.PARTY_LIST and key != "party:cancel":
        return (
            ExpectedState(
                kind="menu_open_cursor",
                surface=R.PARTY_ACTION,
                stable_key="party_action:summary",
            ),
        )
    if scene == R.PARTY_REORDER:
        return (ExpectedState(kind="party_order_changed"),)
    if scene == R.PARTY_ACTION and key == "party_action:item":
        return (
            ExpectedState(
                kind="menu_open_cursor",
                surface=R.PARTY_HELD_ITEM,
                stable_key="party_action:give",
            ),
        )
    if scene == R.PARTY_HELD_ITEM and key == "party_action:take":
        selected = snapshot.ui.menu.party.slot_id  # type: ignore[union-attr]
        mon = next((member for member in snapshot.party if member.slot == selected), None)
        if mon is not None:
            return (
                ExpectedState(
                    kind="held_item_changed",
                    personality=mon.personality,
                    item_id=mon.held_item or None,
                    bag_item_id=mon.held_item or None,
                    bag_delta=1,
                ),
            )
    if scene == R.PARTY_HELD_ITEM and key == "party_action:give":
        if item_id is not None:
            selected = snapshot.ui.menu.party.slot_id  # type: ignore[union-attr]
            mon = next((member for member in snapshot.party if member.slot == selected), None)
            if mon is not None:
                expected = [
                    ExpectedState(
                        kind="held_item_changed",
                        personality=mon.personality,
                        held_item_id=item_id,
                        bag_item_id=item_id,
                        bag_delta=-1,
                    )
                ]
                if mon.held_item not in (0, item_id):
                    # A swap returns the previously held ordinary item to the bag.
                    expected.append(
                        ExpectedState(
                            kind="item_count_changed",
                            item_id=mon.held_item,
                            delta=1,
                        )
                    )
                return tuple(expected)
    return (ExpectedState(kind="menu_closed", surface=scene),)


def _verb(scene: str, entry: MenuEntry) -> str:
    if scene == R.START:
        return START_VERBS.get(entry.stable_key, entry.label.replace(" ", "_"))
    if scene == R.PARTY_LIST:
        return "CANCEL" if entry.stable_key == "party:cancel" else "SELECT_MON"
    if scene == R.PARTY_REORDER:
        return "SWAP_WITH"
    if entry.stable_key.startswith("party_action:field_move:"):
        return "FIELD_MOVE"
    return ACTION_VERBS.get(entry.stable_key, entry.label.replace(" ", "_"))


class StartPartyBuilder:
    def __init__(self, item_options: ItemOptionBuilder | None = None) -> None:
        self.item_options = item_options

    def build(self, ctx: BuildContext) -> FrameDraft:
        scene = ctx.scene.scene_id
        model = _model(ctx.snapshot, scene)
        if model is None or model.surface != scene.replace("party.reorder", "party.list"):
            return FrameDraft(
                options=(
                    Option(
                        id="pause_and_escalate",
                        label="Pause and escalate",
                        description="The current menu labels or cursor are not reliably decoded.",
                        legality=Legality.UNKNOWN,
                    ),
                ),
                state_fingerprint=fingerprint(scene, model="unreadable"),
                context={"summary": "The menu model is unreadable; no input is safe."},
            )
        party_menu = ctx.snapshot.ui.menu.party if ctx.snapshot.ui else None
        if (
            scene == R.PARTY_LIST
            and party_menu is not None
            and getattr(party_menu, "action_name", None) == "use_item"
            and getattr(party_menu, "item_id", None) is not None
        ):
            item_id = int(party_menu.item_id)
            if self.item_options is None:
                if 289 <= item_id <= 346:
                    return self._teach_catalog_unavailable_frame(ctx, model, item_id)
            else:
                try:
                    record = self.item_options.catalog.item(item_id)
                except (KeyError, TypeError, ValueError):
                    record = None
                if record is not None and EffectClass.TEACH_MOVE in record.capability.effects:
                    return self._teach_target_frame(ctx, model, item_id)
        selected = None
        if scene == R.PARTY_REORDER:
            ui = ctx.snapshot.ui
            selected = ui.menu.party.slot_id2 if ui and ui.menu.party else None
        options: list[Option] = []
        give_items: tuple[tuple[int, int], ...] = ()
        if scene == R.PARTY_HELD_ITEM:
            give_items = tuple(
                (item.item_id, item.quantity)
                for pocket in ctx.snapshot.bag
                for item in pocket.items
                if pocket.pocket in {"items", "berries"} and item.quantity > 0 and item.item_id > 0
            )
        for entry in model.entries:
            if scene == R.START and entry.stable_key == "start:bag":
                # No supported field-bag decision builder/detector exists yet; keep the
                # decoded BAG row and its cursor index, but do not offer the choice.
                continue
            mon = next(
                (
                    member
                    for member in ctx.snapshot.party
                    if entry.stable_key == f"party:{member.personality:08x}"
                ),
                None,
            )
            if scene == R.PARTY_REORDER and (mon is None or mon.slot == selected):
                continue
            item_entries: tuple[tuple[int | None, MenuEntry], ...] = ((None, entry),)
            if scene == R.PARTY_HELD_ITEM and entry.stable_key == "party_action:give":
                tables = _tables_from_env()
                item_entries = tuple(
                    (
                        item_id,
                        entry.model_copy(
                            update={
                                "effect": f"give {self._item_name(tables, item_id)} (x{quantity})",
                                "label": f"Give {self._item_name(tables, item_id)} (x{quantity})",
                            }
                        ),
                    )
                    for item_id, quantity in give_items
                )
            for item_id, entry in item_entries:
                options.append(
                    self._option(
                        ctx,
                        scene,
                        entry,
                        mon,
                        selected,
                        item_id=item_id,
                    )
                )
        facts = party_facts(ctx.snapshot)
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=_fingerprint(scene, model, ctx.snapshot),
            context={
                "summary": f"Choose one of {len(options)} decoded {scene} entries.",
                "party": tuple(
                    f"{mon.nickname or mon.species} L{mon.level} HP {mon.hp}/{mon.max_hp}"
                    for mon in ctx.snapshot.party
                ),
                "team_facts": facts,
                "team_facts_text": "\n".join(facts),
            },
        )

    def _teach_target_frame(self, ctx: BuildContext, model: MenuModel, item_id: int) -> FrameDraft:
        assert self.item_options is not None
        details = self.item_options.teach_target_options(ctx.snapshot, item_id)
        options = []
        for detail in details:
            slot = int(detail.executor_args["party_slot"])
            forget = int(detail.executor_args["forget_move"])
            suffix = f":forget:{forget}" if forget else ""
            options.append(
                detail.model_copy(
                    update={
                        "id": f"teach:{item_id}:party:{slot}{suffix}",
                        "executor_ref": "field.finish_teach_tm",
                    }
                )
            )
        item_name = (
            str(details[0].facts.get("item_name", f"item {item_id}"))
            if details
            else f"item {item_id}"
        )
        options.append(
            Option(
                id="cancel",
                label=f"Cancel teaching {item_name}",
                description="Return to the overworld without teaching the move.",
                legality=Legality.LEGAL,
                facts={
                    "menu": "tm_teach_target",
                    "escape": True,
                    "item_id": item_id,
                    "outcome": "returns to the overworld without changing moves",
                },
                executor_ref="field.cancel_teach_tm",
                executor_args={"item_id": item_id},
                postcondition_ref="field.teach_cancelled",
                verb="CANCEL",
                risk=Risk.LOW,
            )
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=_fingerprint(R.PARTY_LIST, model, ctx.snapshot),
            context={
                "summary": f"Choose a legal target/replacement for {item_name}, or cancel.",
                "team_facts": party_facts(ctx.snapshot),
            },
        )

    def _teach_catalog_unavailable_frame(
        self, ctx: BuildContext, model: MenuModel, item_id: int
    ) -> FrameDraft:
        cancel = next(entry for entry in model.entries if entry.stable_key == "party:cancel")
        cancel_option = self._option(ctx, R.PARTY_LIST, cancel, None, None)
        cancel_option = cancel_option.model_copy(
            update={
                "facts": {
                    **cancel_option.facts,
                    "teach_options_unavailable": True,
                    "teach_item_id": item_id,
                    "reason": "ROM item catalog unavailable",
                }
            }
        )
        return FrameDraft(
            options=(cancel_option,),
            state_fingerprint=_fingerprint(R.PARTY_LIST, model, ctx.snapshot),
            context={
                "summary": "TM/HM target choices cannot be decoded without ROM item tables.",
                "teach_options_unavailable": True,
                "teach_item_id": item_id,
                "reason": "ROM item catalog unavailable",
            },
        )

    @staticmethod
    def _item_name(tables: Any, item_id: int) -> str:
        try:
            return str(tables.item_name(item_id)).title()
        except (AttributeError, KeyError, TypeError, ValueError):
            return f"Item {item_id}"

    def _option(self, ctx, scene, entry, mon, selected, *, item_id=None):
        legality = Legality.LEGAL if entry.enabled else Legality.UNAVAILABLE
        option_id = (
            f"swap:{mon.personality:08x}"
            if scene == R.PARTY_REORDER and mon is not None
            else f"give:{re.sub(r'[^a-z0-9]+', '-', entry.label.lower()).strip('-')}"
            if item_id is not None
            else entry.stable_key
        )
        novelty = "opens Pokemon information" if entry.stable_key.endswith("summary") else "n/a"
        return Option(
            id=option_id,
            label=entry.label,
            description=entry.effect or f"Select {entry.label}.",
            legality=legality,
            facts={
                **_facts(ctx, entry, novelty=novelty),
            },
            executor_ref=(
                (R.REF_GIVE if item_id is not None else R.REF_SELECT)
                if legality.chooseable
                else None
            ),
            # A give option names the verified bag item so the registered macro can drive the
            # bag after confirming the cursor entry; other selections carry no arguments.
            executor_args={"item_id": item_id} if item_id is not None else {},
            postcondition_ref=R.POST_EXPECTED if legality.chooseable else None,
            verb=_verb(scene, entry),
            risk=Risk.LOW,
            action_contract=ActionContract(
                cursor_key=entry.stable_key,
                expected=_expected(scene, entry, ctx.snapshot, item_id=item_id),
            )
            if legality.chooseable
            else None,
        )
