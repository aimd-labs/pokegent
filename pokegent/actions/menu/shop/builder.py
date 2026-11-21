"""Option frames for the five supported buy-flow scenes."""

from __future__ import annotations

from typing import Any

from pokegent.actions._exec.contracts import ActionContract, ExpectedState
from pokegent.actions.menu.shop import data as D
from pokegent.actions.menu.shop import evidence as E
from pokegent.actions.menu.shop import refs as R
from pokegent.games.gen3.firered.maps.world import WorldAdapter
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom.rom_tables import (
    POCKET_BALLS,
    POCKET_BERRIES,
    POCKET_ITEMS,
    POCKET_KEY_ITEMS,
    POCKET_TM_HM,
)
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft


def _owned(snapshot: StateSnapshot, item_id: int) -> int:
    for pocket in snapshot.bag:
        for item in pocket.items:
            if item.item_id == item_id:
                return item.quantity
    return 0


def _bag_slot_facts(snapshot: StateSnapshot, item_pocket: int) -> dict[str, Any]:
    """Return live bag facts when the destination pocket is readable."""
    pocket_name = {
        POCKET_ITEMS: "items",
        POCKET_KEY_ITEMS: "key_items",
        POCKET_BALLS: "poke_balls",
        POCKET_TM_HM: "tms_hms",
        POCKET_BERRIES: "berries",
    }.get(item_pocket)
    pocket = next((pocket for pocket in snapshot.bag if pocket.pocket == pocket_name), None)
    if pocket is None:
        return {}
    return {
        "bag_capacity": pocket.capacity,
        "bag_count": len(pocket.items),
    }


def _sell_item_facts(tables: Any, item_id: int) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if (move_id := tables.tmhm_move(item_id)) is not None:
        facts["teaches"] = tables.move_name(move_id)
    if any(
        tables.evolution_for_item(species, item_id) for species in range(len(tables.all_species))
    ):
        facts["evolution_item"] = True
    return facts


def _option(
    option_id: str,
    label: str,
    description: str,
    legality: Legality,
    *,
    facts: dict[str, Any] | None = None,
    executor_ref: str | None = None,
    executor_args: dict[str, Any] | None = None,
    postcondition_ref: str | None = None,
    verb: str | None = None,
    cursor_key: str | None = None,
    expected: tuple[ExpectedState, ...] = (),
) -> Option:
    escape = (
        (verb or "").upper() in {"CANCEL", "BACK", "CLOSE", "LOG_OFF"}
        or any(token in option_id.lower() for token in ("cancel", "back", "close", "log_off"))
        or any(token in (executor_ref or "").upper() for token in ("LIST_BACK", "QUANTITY_BACK"))
    )
    return Option(
        id=option_id,
        label=label,
        description=description,
        legality=legality,
        facts={**(facts or {}), **({"escape": True} if escape else {})},
        executor_ref=executor_ref,
        executor_args=executor_args or {},
        postcondition_ref=postcondition_ref,
        verb=verb,
        risk=Risk.LOW,
        action_contract=(
            ActionContract(cursor_key=cursor_key, expected=expected)
            if cursor_key is not None or expected
            else None
        ),
    )


def _missing(scene: str, reason: str) -> FrameDraft:
    return FrameDraft(
        options=(_option("pause_and_escalate", "Pause and escalate", reason, Legality.UNKNOWN),),
        state_fingerprint=fingerprint(scene, missing=reason),
        context={"summary": reason},
    )


class ShopBuilder:
    def __init__(self, world: WorldAdapter | None = None) -> None:
        self.world = world
        self.tables = world.tables if world is not None else None

    def build(self, ctx: BuildContext) -> FrameDraft:
        scene = ctx.scene.scene_id
        if scene == R.SHOP_PROMPT:
            return self.main(ctx.snapshot, scene_id=R.SHOP_PROMPT)
        if scene == R.MAIN:
            return self.main(ctx.snapshot)
        if scene == R.BUY_LIST:
            return self.buy_list(ctx.snapshot, objective_item=ctx.objective_item)
        if scene == R.BUY_QUANTITY:
            return self.buy_quantity(
                ctx.snapshot,
                objective_item=ctx.objective_item,
                objective_quantity=ctx.objective_quantity,
            )
        if scene == R.BUY_CONFIRM:
            return self.buy_confirm(ctx.snapshot)
        if scene == R.BUY_DONE:
            return self.buy_done(ctx.snapshot)
        if scene == R.SELL_LIST:
            return self.sell_list(ctx.snapshot)
        if scene == R.SELL_QUANTITY:
            return self.sell_quantity(ctx.snapshot)
        if scene == R.SELL_CONFIRM:
            return self.sell_confirm(ctx.snapshot)
        if scene == R.SELL_DONE:
            return self.sell_done(ctx.snapshot)
        return _missing(scene, "the shop family does not own this screen")

    def main(self, snapshot: StateSnapshot, *, scene_id: str = R.MAIN) -> FrameDraft:
        return FrameDraft(
            options=(
                _option(
                    "buy",
                    "BUY",
                    "Open the shop's buy list.",
                    Legality.LEGAL,
                    executor_ref=(
                        R.REF_PROMPT_CURSOR if scene_id == R.SHOP_PROMPT else R.REF_MAIN_CURSOR
                    ),
                    executor_args={"target": 0},
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="BUY",
                    cursor_key="shop.main:buy",
                    expected=(ExpectedState(kind="menu_closed", surface=R.MAIN),),
                ),
                _option(
                    "quit",
                    "QUIT",
                    "Leave the shop without selling anything.",
                    Legality.LEGAL,
                    executor_ref=R.REF_MAIN_CURSOR,
                    executor_args={"target": 2},
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="QUIT",
                    cursor_key="shop.main:quit",
                    expected=(ExpectedState(kind="menu_closed", surface=R.MAIN),),
                ),
                _option(
                    "sell",
                    "SELL",
                    "Open the bag and choose an item to sell.",
                    Legality.LEGAL,
                    executor_ref=R.REF_MAIN_CURSOR,
                    executor_args={"target": 1},
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="SELL",
                    cursor_key="shop.main:sell",
                    expected=(ExpectedState(kind="menu_closed", surface=R.MAIN),),
                ),
            ),
            state_fingerprint=fingerprint(
                scene_id, cursor=_cursor(snapshot), money=_money(snapshot)
            ),
            context={
                "summary": "Poke Mart clerk prompt. Choose BUY, SELL, or QUIT.",
                "money": _money(snapshot),
            },
        )

    def sell_list(self, snapshot: StateSnapshot) -> FrameDraft:
        if self.tables is None:
            return _missing(R.SELL_LIST, "ROM item data is unavailable")
        options = []
        native_pockets = {"items": 0, "key_items": 1, "poke_balls": 2}
        unsupported_pocket_labels = {"tms_hms": "TM Case", "berries": "Berry Pouch"}
        entries = (
            (pocket, index, entry)
            for pocket in snapshot.bag
            for index, entry in enumerate(pocket.items)
        )
        for pocket, index, entry in entries:
            name, price, protected = D.sell_item(self.tables, entry.item_id, owned=entry.quantity)
            reason = protected or (
                f"sell path not implemented for {pocket.pocket} "
                f"({unsupported_pocket_labels.get(pocket.pocket, pocket.pocket)})"
                if pocket.pocket not in native_pockets
                else None
            )
            legal = Legality.UNAVAILABLE if reason else Legality.LEGAL
            facts = {
                "item_id": entry.item_id,
                "owned": entry.quantity,
                "price": price,
                "sell_price": price,
                "you_keep": entry.quantity - 1,
                "protected_reason": reason,
            }
            facts.update(_sell_item_facts(self.tables, entry.item_id))
            options.append(
                _option(
                    f"sell:{entry.item_id}:1",
                    f"Sell {name}",
                    f"Sell one {name}; sells for {price} each."
                    if not reason
                    else f"Cannot sell {name}: {reason}.",
                    legal,
                    facts=facts,
                    executor_ref=R.REF_SELL_ITEM if legal.chooseable else None,
                    executor_args={
                        "item_ref": entry.item_id,
                        "pocket_index": native_pockets.get(pocket.pocket, -1),
                        "item_index": index,
                    },
                    postcondition_ref=None,
                    verb="SELL_ITEM",
                    cursor_key=f"{R.SELL_LIST}:{entry.item_id}" if legal.chooseable else None,
                    expected=(ExpectedState(kind="menu_closed", surface=R.SELL_LIST),)
                    if legal.chooseable
                    else (),
                )
            )
        options.append(
            _option(
                "back",
                "BACK",
                "Return to the shop main menu without selling.",
                Legality.LEGAL,
                executor_ref=R.REF_SELL_LIST_BACK,
                postcondition_ref=R.POST_MOVED_ON,
                verb="CANCEL",
                facts={"escape": True},
            )
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                R.SELL_LIST, bag=_bag_fingerprint(snapshot), money=_money(snapshot)
            ),
            context={
                "summary": "Choose a non-protected bag item to sell.",
                "money": _money(snapshot),
            },
        )

    def sell_quantity(self, snapshot: StateSnapshot) -> FrameDraft:
        item_id, current = E.selected_item(snapshot), E.quantity(snapshot)
        if self.tables is None:
            return _missing(R.SELL_QUANTITY, "ROM item data is unavailable")
        name, price, protected = D.sell_item(
            self.tables, item_id or -1, owned=_owned(snapshot, item_id or -1)
        )
        if item_id is None or current is None:
            return _missing(R.SELL_QUANTITY, "selected sell item or quantity is not readable")
        owned = _owned(snapshot, item_id)
        maximum = owned
        options = [
            _option(
                f"sell:{item_id}:{value}",
                f"Sell {value} {name}",
                f"Sell {value}; sells for {price} each ({price * value} total).",
                Legality.LEGAL,
                facts={
                    "item_id": item_id,
                    "quantity": value,
                    "sell_price": price,
                    "total": price * value,
                    "you_keep": owned - value,
                    **_sell_item_facts(self.tables, item_id),
                },
                executor_ref=R.REF_SELL_QUANTITY,
                executor_args={"target_quantity": value, "item_ref": item_id, "unit_price": price},
                postcondition_ref=R.POST_MOVED_ON,
                verb="SET_QUANTITY",
            )
            for value in sorted({1, 2, 3, 5, maximum})
            if 1 <= value <= maximum and not protected
        ]
        options.append(
            _option(
                "back",
                "BACK",
                "Return without selling.",
                Legality.LEGAL,
                executor_ref=R.REF_QUANTITY_BACK,
                postcondition_ref=R.POST_MOVED_ON,
                verb="CANCEL",
                facts={"escape": True},
            )
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                R.SELL_QUANTITY, item=item_id, current=current, money=_money(snapshot)
            ),
            context={"summary": f"Choose how many {name} to sell.", "item": name},
        )

    def sell_confirm(self, snapshot: StateSnapshot) -> FrameDraft:
        item_id, qty = E.selected_item(snapshot), E.quantity(snapshot)
        if self.tables is None:
            return _missing(R.SELL_CONFIRM, "ROM item data is unavailable")
        name, price, protected = D.sell_item(
            self.tables, item_id or -1, owned=_owned(snapshot, item_id or -1)
        )
        owned = _owned(snapshot, item_id or -1)
        if item_id is None or qty is None or price <= 0:
            return _missing(R.SELL_CONFIRM, "the sell quote is not legal or readable")
        if qty < 1 or qty > owned:
            return _missing(
                R.SELL_CONFIRM,
                f"cannot sell {qty}; you have {owned} (sellable: {owned})",
            )
        if protected:
            return _missing(R.SELL_CONFIRM, f"cannot sell this item: {protected}")
        total = price * qty
        facts = {
            "item_id": item_id,
            "quantity": qty,
            "sell_price": price,
            "total": total,
            "money": _money(snapshot),
            "money_after": (_money(snapshot) or 0) + total,
            "you_keep": owned - qty,
            **_sell_item_facts(self.tables, item_id),
        }
        return FrameDraft(
            options=(
                _option(
                    "yes",
                    "YES",
                    f"Accept: I can pay you {total}.",
                    Legality.LEGAL,
                    facts=facts,
                    executor_ref=R.REF_SELL_CONFIRM,
                    executor_args={
                        "item_ref": item_id,
                        "target_quantity": qty,
                        "unit_price": price,
                    },
                    postcondition_ref=R.POST_SELL_TRANSACTION,
                    verb="YES",
                    cursor_key="yes",
                    expected=(
                        ExpectedState(kind="item_count_changed", item_id=item_id, delta=-qty),
                    ),
                ),
                _option(
                    "no",
                    "NO",
                    "Reject the sell quote and return to the item list.",
                    Legality.LEGAL,
                    facts=facts,
                    executor_ref=R.REF_SELL_CONFIRM_NO,
                    executor_args={"target": 1},
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="NO",
                    cursor_key="no",
                    expected=(ExpectedState(kind="menu_closed", surface=R.SELL_CONFIRM),),
                ),
            ),
            state_fingerprint=fingerprint(
                R.SELL_CONFIRM, item=item_id, quantity=qty, money=_money(snapshot)
            ),
            context={
                "summary": f"I can pay you {total}. Would that be OK?",
                "item": name,
                "quantity": qty,
                "total": total,
            },
        )

    def sell_done(self, snapshot: StateSnapshot) -> FrameDraft:
        return FrameDraft(
            options=(
                _option(
                    "continue_shopping",
                    "CONTINUE SHOPPING",
                    "Return to the sell list and sell another item.",
                    Legality.LEGAL,
                    executor_ref=R.REF_SELL_DONE_CONTINUE,
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="CONTINUE_SHOPPING",
                ),
                _option(
                    "quit",
                    "QUIT",
                    "Leave after the verified sale.",
                    Legality.LEGAL,
                    executor_ref=R.REF_SELL_DONE_QUIT,
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="QUIT",
                ),
            ),
            state_fingerprint=fingerprint(
                R.SELL_DONE, money=_money(snapshot), bag=_bag_fingerprint(snapshot)
            ),
            context={"summary": "The sale result is verified. Continue or leave."},
        )

    def buy_list(self, snapshot: StateSnapshot, *, objective_item: int | None = None) -> FrameDraft:
        if snapshot.player is None:
            return _missing(R.BUY_LIST, "player money and map identity are unreadable")
        inventory = self.world.shop_inventory(snapshot) if self.world is not None else None
        if inventory is None:
            return _missing(R.BUY_LIST, "this mart inventory is not present in the supported ROM")
        options: list[Option] = []
        objective_unmet_reason: str | None = None
        if objective_item is not None and all(
            offer.item_id != objective_item for offer in inventory
        ):
            target = self._item(objective_item)
            item_name = target.name if target is not None else f"item {objective_item}"
            objective_unmet_reason = (
                f"{item_name} is not stocked at the current mart on map "
                f"{snapshot.player.map_group}/{snapshot.player.map_num}"
            )
        for index, offer in enumerate(inventory):
            item = self.tables.item(offer.item_id)
            facts = D.item_facts(
                item,
                owned=offer.owned,
                money=snapshot.player.money,
                need=offer.need,
            )
            facts.update(_bag_slot_facts(snapshot, item.pocket))
            if objective_item is not None:
                facts["shop_serves_goal"] = item.item_id == objective_item
            unavailable_reason: str | None = None
            if not facts["money_sufficient"]:
                unavailable_reason = (
                    f"not enough money: have {snapshot.player.money}, price {item.price}"
                )
            if unavailable_reason is not None:
                facts["unavailable_reason"] = unavailable_reason
                if item.item_id == objective_item:
                    objective_unmet_reason = (
                        f"cannot afford {item.name}: have {snapshot.player.money}, "
                        f"need {item.price}"
                    )
            legal = Legality.LEGAL if facts["money_sufficient"] else Legality.UNAVAILABLE
            effect = facts["battle_effect"] or "has no supported battle-use effect"
            description = f"Buy one {item.name}; {effect}."
            if unavailable_reason is not None:
                description = f"Cannot buy {item.name}: {unavailable_reason}."
            options.append(
                _option(
                    f"buy:{item.item_id}",
                    f"Buy {item.name}",
                    description,
                    legal,
                    facts=facts,
                    executor_ref=R.REF_LIST_ITEM if legal.chooseable else None,
                    executor_args={"item_id": item.item_id, "item_index": index},
                    postcondition_ref=R.POST_MOVED_ON if legal.chooseable else None,
                    verb="BUY_ITEM",
                    cursor_key=f"{R.BUY_LIST}:{item.item_id}" if legal.chooseable else None,
                    expected=(ExpectedState(kind="menu_closed", surface=R.BUY_LIST),)
                    if legal.chooseable
                    else (),
                )
            )
        options.append(
            _option(
                "back",
                "BACK",
                "Return to the shop main menu.",
                Legality.LEGAL,
                executor_ref=R.REF_LIST_BACK,
                postcondition_ref=R.POST_MOVED_ON,
                verb="CANCEL",
                facts={"escape": True},
            )
        )
        selected = E.classify(snapshot).evidence if E.classify(snapshot) else {}
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                R.BUY_LIST,
                map=(snapshot.player.map_group, snapshot.player.map_num),
                cursor=selected.get("selected_index"),
                money=snapshot.player.money,
                bag=_bag_fingerprint(snapshot),
            ),
            context={
                "summary": "Choose one item from the verified mart inventory.",
                "money": snapshot.player.money,
                **selected,
                **(
                    {"objective_unmet_reason": objective_unmet_reason}
                    if objective_unmet_reason is not None
                    else {}
                ),
            },
        )

    def buy_quantity(
        self,
        snapshot: StateSnapshot,
        *,
        objective_item: int | None = None,
        objective_quantity: int | None = None,
    ) -> FrameDraft:
        item_id, current = E.selected_item(snapshot), E.quantity(snapshot)
        item = self._item(item_id)
        money = _money(snapshot)
        if item is None or current is None:
            return _missing(R.BUY_QUANTITY, "selected item or quantity is not readable")
        # The native quantity prompt permits every quantity affordable by the live
        # money value.  Keeping a policy reserve here hid legal quantities (including
        # quantity one) and was not a ROM fact.
        spendable = max(0, money)
        maximum = min(99, spendable // item.price)
        values = sorted({1, 2, 3, 5, maximum})
        objective_remaining: int | None = None
        objective_serving_quantity: int | None = None
        objective_affordable_max: int | None = None
        if objective_item == item.item_id and objective_quantity is not None:
            objective_remaining = max(0, objective_quantity - _owned(snapshot, item.item_id))
            if objective_remaining > 0 and objective_remaining <= 99:
                objective_serving_quantity = min(objective_remaining, maximum)
                objective_affordable_max = maximum if maximum < objective_remaining else None
                if objective_serving_quantity > 0:
                    values = sorted({*values, objective_serving_quantity})
        options = []
        for value in values:
            if value < 1:
                continue
            total = value * item.price
            legal = Legality.LEGAL if total <= spendable else Legality.UNAVAILABLE
            facts = {
                "item_id": item.item_id,
                "quantity": value,
                "price": item.price,
                "total_cost": total,
                "money": money,
                "spendable_money": spendable,
            }
            if objective_item == item.item_id and objective_quantity is not None:
                owned = _owned(snapshot, item.item_id)
                facts.update(
                    {
                        "objective_owned": owned,
                        "objective_after": owned + value,
                        "objective_target": objective_quantity,
                    }
                )
                if value == objective_serving_quantity:
                    facts["shop_serves_goal"] = True
                    if objective_affordable_max is not None:
                        facts["objective_limit_reason"] = (
                            f"affordable max {objective_affordable_max}"
                        )
            options.append(
                _option(
                    f"quantity:{value}",
                    f"Buy {value} {item.name}",
                    f"Set the quantity to {value}.",
                    legal,
                    facts=facts,
                    executor_ref=R.REF_QUANTITY if legal.chooseable else None,
                    executor_args={
                        "target_quantity": value,
                        "item_ref": item.item_id,
                        "unit_cost": item.price,
                    },
                    postcondition_ref=R.POST_MOVED_ON if legal.chooseable else None,
                    verb="SET_QUANTITY",
                )
            )
        options.append(
            _option(
                "back",
                "BACK",
                "Return to the buy list without buying.",
                Legality.LEGAL,
                executor_ref=R.REF_QUANTITY_BACK,
                postcondition_ref=R.POST_MOVED_ON,
                verb="CANCEL",
                facts={"escape": True},
            )
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                R.BUY_QUANTITY, item=item.item_id, current=current, money=money
            ),
            context={
                "summary": f"How many {item.name} would you like?",
                "item": item.name,
                "current_quantity": current,
                "money": money,
            },
        )

    def buy_confirm(self, snapshot: StateSnapshot) -> FrameDraft:
        item_id = E.selected_item(snapshot)
        item = self._item(item_id)
        if item is None:
            return _missing(R.BUY_CONFIRM, "the selected item is not readable")
        quantity = E.quantity(snapshot) or 1
        total = item.price * quantity
        facts = {
            "item_id": item.item_id,
            "quantity": quantity,
            "price": item.price,
            "total_cost": total,
            "money": _money(snapshot),
            "money_after": _money(snapshot) - total,
        }
        return FrameDraft(
            options=(
                _option(
                    "yes",
                    "YES",
                    f"Confirm buying {quantity} {item.name}.",
                    Legality.LEGAL,
                    facts=facts,
                    executor_ref=R.REF_CONFIRM,
                    executor_args={
                        "item_ref": item.item_id,
                        "target_quantity": quantity,
                        "unit_cost": item.price,
                    },
                    postcondition_ref=R.POST_TRANSACTION,
                    verb="YES",
                    cursor_key="yes",
                    expected=(
                        ExpectedState(
                            kind="item_count_changed", item_id=item.item_id, delta=quantity
                        ),
                    ),
                ),
                _option(
                    "no",
                    "NO",
                    "Cancel this purchase and return to the buy list.",
                    Legality.LEGAL,
                    facts=facts,
                    executor_ref=R.REF_CONFIRM_NO,
                    postcondition_ref=R.POST_MOVED_ON,
                    executor_args={"target": 1},
                    verb="NO",
                    cursor_key="no",
                    expected=(ExpectedState(kind="menu_closed", surface=R.BUY_CONFIRM),),
                ),
            ),
            state_fingerprint=fingerprint(
                R.BUY_CONFIRM, item=item.item_id, quantity=quantity, money=_money(snapshot)
            ),
            context={
                "summary": f"Confirm {quantity} {item.name} for {total} money.",
                "item": item.name,
                "quantity": quantity,
                "total_cost": total,
            },
        )

    def buy_done(self, snapshot: StateSnapshot) -> FrameDraft:
        return FrameDraft(
            options=(
                _option(
                    "continue_shopping",
                    "CONTINUE SHOPPING",
                    "Return to the shop main menu and buy another item.",
                    Legality.LEGAL,
                    executor_ref=R.REF_DONE_CONTINUE,
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="CONTINUE_SHOPPING",
                ),
                _option(
                    "quit",
                    "QUIT",
                    "Leave the shop after the verified purchase.",
                    Legality.LEGAL,
                    executor_ref=R.REF_DONE_QUIT,
                    postcondition_ref=R.POST_MOVED_ON,
                    verb="QUIT",
                ),
            ),
            state_fingerprint=fingerprint(
                R.BUY_DONE, money=_money(snapshot), bag=_bag_fingerprint(snapshot)
            ),
            context={
                "summary": "The purchase result is on screen. Choose whether to continue or leave."
            },
        )

    def _item(self, item_id: int | None):
        if self.tables is None or item_id is None:
            return None
        try:
            return self.tables.item(item_id)
        except KeyError:
            return None


def _money(snapshot: StateSnapshot) -> int | None:
    return snapshot.player.money if snapshot.player is not None else None


def _cursor(snapshot: StateSnapshot) -> int | None:
    ui = snapshot.ui
    return ui.menu.menu.cursor_pos if ui and ui.menu and ui.menu.menu else None


def _bag_fingerprint(
    snapshot: StateSnapshot,
) -> tuple[tuple[str, tuple[tuple[int, int], ...]], ...]:
    """Keep the executor fingerprint JSON-like while retaining all bag quantities."""
    return tuple(
        (pocket.pocket, tuple((item.item_id, item.quantity) for item in pocket.items))
        for pocket in snapshot.bag
    )
