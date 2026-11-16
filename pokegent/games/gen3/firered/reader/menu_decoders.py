"""FireRed menu decoders for RAM-backed menu execution.

The decoder consumes one :class:`StateSnapshot` and the matching atomic memory port.  It never
presses a key and never supplies options or executors to the live loop.  Unknown pointers, missing
symbols, malformed text and out-of-range ids are represented by an absent model rather than a
guessed label.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from pokegent.actions._exec.contracts import MenuEntry, MenuModel, PromptModel
from pokegent.actions._exec.prompt import build_prompt_model
from pokegent.actions.battle.romtables import RomTables
from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, MemoryReadError
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.dialog_text import DialogText
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

MAX_MENU_ENTRIES = 128
LIST_MENU_ITEM_SIZE = 8  # struct ListMenuItem: const u8 *name; u32 id; compiler padding
MAX_LABEL_BYTES = 32
ROM_START = 0x0800_0000
ROM_END = 0x0A00_0000
EWRAM_START = 0x0200_0000
EWRAM_END = 0x0204_0000
IWRAM_START = 0x0300_0000
IWRAM_END = 0x0300_8000

START_LABELS = ("POKEDEX", "POKEMON", "BAG", "PLAYER CARD", "SAVE", "OPTIONS", "EXIT")
START_TARGETS = {
    "POKEDEX": {"scene": "pokedex.list", "callback": "CB2_PokedexScreen", "recovery_only": True},
    "POKEMON": {"scene": "party.list", "callback": "CB2_UpdatePartyMenu", "family": "start_party"},
    "BAG": {"scene": "bag.items", "callback": "CB2_BagMenuRun", "family": "field"},
    "PLAYER CARD": {
        "scene": "menu.trainer_card",
        "callback": "CB2_TrainerCard",
        "recovery_only": True,
    },
    "SAVE": {"scene": "menu.save", "callback": "CB2_Overworld", "recovery_only": True},
    "OPTIONS": {"scene": "menu.options", "callback": "CB2_OptionMenu", "recovery_only": True},
    "EXIT": {"scene": "field.free", "callback": "CB2_Overworld", "family": "field"},
}


def _bounded_pointer(address: int, size: int) -> bool:
    return any(
        start <= address and address + size <= end
        for start, end in ((ROM_START, ROM_END), (EWRAM_START, EWRAM_END), (IWRAM_START, IWRAM_END))
    )


def _read_label(port: MemoryPort, pointer: int) -> str | None:
    if not _bounded_pointer(pointer, MAX_LABEL_BYTES):
        return None
    raw = bytearray()
    try:
        for offset in range(MAX_LABEL_BYTES):
            byte = port.read_u8(pointer + offset)
            raw.append(byte)
            if byte == 0xFF:
                break
        else:
            return None
    except (MemoryReadError, ValueError, struct.error):
        return None
    try:
        return decode_gen3_text(bytes(raw)) or None
    except (UnicodeError, ValueError):
        return None


def decode_list_menu(
    port: MemoryPort,
    *,
    list_menu: object,
    surface: str,
    fallback_name: Callable[[int], str | None] | None = None,
    stable_keys: tuple[str, ...] | None = None,
    disabled_item_ids: frozenset[int] = frozenset({0}),
) -> MenuModel | None:
    """Decode a ListMenu item table with strict count, pointer and string bounds."""
    total = getattr(list_menu, "total_items", 0)
    pointer = getattr(list_menu, "items_ptr", None)
    if not isinstance(total, int) or not 0 <= total <= MAX_MENU_ENTRIES:
        return None
    if stable_keys is not None and len(stable_keys) != total:
        return None
    if not isinstance(pointer, int) or not _bounded_pointer(pointer, total * LIST_MENU_ITEM_SIZE):
        return None
    entries: list[MenuEntry] = []
    seen_keys: set[str] = set()
    for index in range(total):
        try:
            row = port.read_range(pointer + index * LIST_MENU_ITEM_SIZE, LIST_MENU_ITEM_SIZE)
            label_ptr, item_id = struct.unpack_from("<II", row)
        except (MemoryReadError, ValueError, struct.error):
            return None
        label = _read_label(port, label_ptr) if label_ptr else None
        if label is None and fallback_name is not None:
            label = fallback_name(item_id)
        if label is None:
            return None
        stable_key = (
            stable_keys[index]
            if stable_keys is not None and index < len(stable_keys)
            else f"{surface}:{item_id}"
        )
        if stable_key in seen_keys:
            # A duplicate item id is only safe when the ROM supplied a distinct label.  The
            # label is a game identity; the row number is deliberately not part of the key.
            stable_key = f"{stable_key}:{label}"
        if stable_key in seen_keys:
            return None
        seen_keys.add(stable_key)
        entries.append(
            MenuEntry(
                stable_key=stable_key,
                label=label,
                effect=f"select {label}",
                enabled=item_id not in disabled_item_ids,
            )
        )
    selected = getattr(list_menu, "scroll_offset", 0) + getattr(list_menu, "cursor_row", 0)
    selected_key = entries[selected].stable_key if 0 <= selected < len(entries) else None
    return MenuModel(
        surface=surface,
        entries=tuple(entries),
        selected_key=selected_key,
        scroll=max(0, int(getattr(list_menu, "scroll_offset", 0))),
        cancel=True,
    )


def decode_cursor_menu(
    snapshot: StateSnapshot,
    *,
    surface: str,
    entries: tuple[tuple[str, str], ...],
) -> MenuModel | None:
    """Decode an ``sMenu`` vertical cursor whose entry identities are supplied by its owner."""
    ui = snapshot.ui
    menu = ui.menu.menu if ui and ui.valid else None
    if menu is None or not menu.window_in_use or not menu.consistent:
        return None
    count = menu.max_cursor_pos - menu.min_cursor_pos + 1
    if count != len(entries) or not 0 < count <= MAX_MENU_ENTRIES:
        return None
    models = tuple(
        MenuEntry(stable_key=key, label=label, effect=f"select {label}") for key, label in entries
    )
    selected = menu.cursor_pos - menu.min_cursor_pos
    return MenuModel(
        surface=surface,
        entries=models,
        selected_key=models[selected].stable_key if 0 <= selected < len(models) else None,
        cancel=True,
    )


def decode_multichoice_menu(
    snapshot: StateSnapshot,
    port: MemoryPort,
    profile: RomProfile,
    *,
    surface: str,
) -> MenuModel | None:
    ui = snapshot.ui
    menu = ui.menu.menu if ui and ui.valid else None
    multichoice = ui.menu.multichoice if ui and ui.valid else None
    if menu is None or multichoice is None:
        return None
    count = menu.max_cursor_pos - menu.min_cursor_pos + 1
    from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver

    resolved = FireRedMenuResolver(port, profile).multichoice(
        snapshot, multichoice.multichoice_id, count
    )
    if resolved is None:
        return None
    entries = tuple((entry.stable_key, entry.label) for entry in resolved)
    return decode_cursor_menu(snapshot, surface=surface, entries=entries)


def decode_shop_menu(
    snapshot: StateSnapshot,
    port: MemoryPort,
    profile: RomProfile,
    *,
    scene_id: str,
) -> MenuModel | None:
    """Decode cursor-bearing shop phases without changing the shop family's live macros."""
    if scene_id == "shop.main":
        return decode_cursor_menu(
            snapshot,
            surface=scene_id,
            entries=(
                ("shop.main:buy", "BUY"),
                ("shop.main:sell", "SELL"),
                ("shop.main:quit", "QUIT"),
            ),
        )
    if scene_id in ("shop.buy_confirm", "shop.sell_confirm"):
        return decode_cursor_menu(
            snapshot,
            surface=scene_id,
            entries=(("yes", "YES"), ("no", "NO")),
        )
    ui = snapshot.ui
    list_menu = ui.menu.list_menu if ui and ui.valid else None
    if list_menu is None or not list_menu.window_in_use:
        return None
    if scene_id == "shop.buy_list":
        return decode_list_menu(
            port,
            list_menu=list_menu,
            surface=scene_id,
            fallback_name=RomTables(port, profile, use_generated=True).item_name,
            disabled_item_ids=frozenset(),
        )
    if scene_id == "shop.sell_list":
        tables = RomTables(port, profile, use_generated=True)
        bag_items = tuple(item for pocket in snapshot.bag for item in pocket.items)
        rows = [
            MenuEntry(
                stable_key=f"{scene_id}:{item.item_id}",
                label=tables.item_name(item.item_id) or f"ITEM_{item.item_id}",
                effect=f"select item {item.item_id}",
            )
            for item in bag_items
        ]
        if list_menu.total_items == len(rows) + 1:
            rows.append(MenuEntry(stable_key=f"{scene_id}:back", label="BACK", effect="close list"))
        if len(rows) != list_menu.total_items:
            return None
        selected = list_menu.scroll_offset + list_menu.cursor_row
        return MenuModel(
            surface=scene_id,
            entries=tuple(rows),
            selected_key=rows[selected].stable_key if 0 <= selected < len(rows) else None,
            scroll=list_menu.scroll_offset,
            cancel=True,
        )
    return None


def decode_elevator_menu(
    snapshot: StateSnapshot,
    port: MemoryPort,
    *,
    scene_id: str,
) -> MenuModel | None:
    from pokegent.actions.special.elevators import _elevator

    found = _elevator(snapshot)
    if found is None:
        return None
    name, data = found
    identities = tuple(
        (f"elevator:{name}:{floor['code']}", str(floor["label"])) for floor in data["floors"]
    )
    if scene_id == "dialog.elevator":
        return decode_cursor_menu(
            snapshot,
            surface=scene_id,
            entries=tuple(
                (f"choice:{entry}", label) for entry, (_key, label) in enumerate(identities)
            ),
        )
    ui = snapshot.ui
    list_menu = ui.menu.list_menu if ui and ui.valid else None
    if list_menu is None or not list_menu.window_in_use:
        return None
    return decode_list_menu(
        port,
        list_menu=list_menu,
        surface=scene_id,
        stable_keys=tuple(key for key, _label in identities),
        disabled_item_ids=frozenset(),
    )


def decode_start_menu(snapshot: StateSnapshot) -> MenuModel | None:
    """Decode the fixed FireRed START labels using the RAM-reported item count."""
    ui = snapshot.ui
    menu = ui.menu.start_menu if ui and ui.valid else None
    if menu is None or menu.task_active is not True:
        return None
    count = menu.num_items
    if snapshot.flags is None:
        return None
    labels = START_LABELS if snapshot.flags.is_set(C.FLAG_SYS_POKEDEX_GET) else START_LABELS[1:]
    if not 0 < count <= len(labels):
        return None
    entries = tuple(
        MenuEntry(
            stable_key=f"start:{label.lower().replace(' ', '_')}",
            label=label,
            effect=f"open {label}",
            target_screen=START_TARGETS[label],
        )
        for label in labels[:count]
    )
    selected = menu.cursor_pos
    return MenuModel(
        surface="menu.start",
        entries=entries,
        selected_key=entries[selected].stable_key if 0 <= selected < len(entries) else None,
        scroll=0,
        cancel=True,
    )


def decode_party_menu(snapshot: StateSnapshot, tables: RomTables | None = None) -> MenuModel | None:
    ui = snapshot.ui
    menu = ui.menu.party if ui and ui.valid else None
    if menu is None or menu.active_hint is not True or not snapshot.health.valid:
        return None
    party = menu
    entries_list: list[MenuEntry] = []
    for member in snapshot.party:
        label = member.nickname or (tables.species_name(member.species) if tables else None)
        if label is None:
            return None
        entries_list.append(
            MenuEntry(
                stable_key=f"party:{member.personality:08x}",
                label=label,
                effect=f"select party slot {member.slot}",
                enabled=not member.is_egg,
            )
        )
    if not entries_list:
        return None
    if party.forced_send_out is not True:
        entries_list.append(
            MenuEntry(
                stable_key="party:cancel",
                label="BACK",
                effect="close party list",
            )
        )
    selected = menu.slot_id
    return MenuModel(
        surface="party.list",
        entries=tuple(entries_list),
        selected_key=next(
            (e.stable_key for e, m in zip(entries_list, snapshot.party) if m.slot == selected),
            None,
        ),
        scroll=0,
        cancel=menu.forced_send_out is not True,
    )


def decode_storage_menu(snapshot: StateSnapshot, *, operation: int) -> MenuModel | None:
    """Decode the storage grid/party strip from settled cursor and current identities."""
    storage = snapshot.storage
    if (
        storage is None
        or storage.box_option != operation
        or not storage.cursor_settled
        or storage.cursor_position is None
    ):
        return None
    if operation == 0 or (operation == 2 and storage.cursor_area == 0):
        if storage.cursor_area != 0:
            return None
        mons = storage.boxes[storage.current_box]
        surface = "pc.storage.move" if operation == 2 else "pc.storage.withdraw"
        entries = tuple(
            MenuEntry(
                stable_key=(
                    f"pokemon:{int(mon.personality)}"
                    if mon is not None
                    else f"empty:box:{storage.current_box}:{slot}"
                ),
                label=(getattr(mon, "nickname", "") or f"Pokemon {int(mon.species)}")
                if mon is not None
                else "EMPTY",
                effect=f"select box slot {slot}",
                enabled=mon is not None or operation == 2,
            )
            for slot, mon in enumerate(mons)
        )
    elif operation == 1 or (operation == 2 and storage.cursor_area == 1):
        if storage.cursor_area != 1:
            return None
        by_slot = {int(mon.slot): mon for mon in snapshot.party}
        surface = "pc.storage.move_party" if operation == 2 else "pc.storage.deposit"
        entries = tuple(
            MenuEntry(
                stable_key=(
                    f"party:{slot}"
                    if operation == 2
                    else (
                        f"pokemon:{int(mon.personality)}"
                        if mon is not None
                        else f"empty:party:{slot}"
                    )
                ),
                label=(getattr(mon, "nickname", "") or f"Pokemon {int(mon.species)}")
                if mon is not None
                else "EMPTY",
                effect=f"select party slot {slot}",
                enabled=mon is not None,
            )
            for slot in range(C.PARTY_SIZE)
            for mon in (by_slot.get(slot),)
        )
    else:
        return None
    selected = storage.cursor_position
    return MenuModel(
        surface=surface,
        entries=entries,
        selected_key=entries[selected].stable_key if 0 <= selected < len(entries) else None,
        cancel=True,
    )


def decode_party_reorder(
    snapshot: StateSnapshot, tables: RomTables | None = None
) -> MenuModel | None:
    """Decode SWITCH target selection, whose live cursor is ``slot_id2`` not ``slot_id``."""
    model = decode_party_menu(snapshot, tables)
    ui = snapshot.ui
    party = ui.menu.party if ui and ui.valid else None
    if model is None or party is None or party.action_name != "switch":
        return None
    selected = next(
        (
            f"party:{member.personality:08x}"
            for member in snapshot.party
            if member.slot == party.slot_id2
        ),
        None,
    )
    return model.model_copy(update={"selected_key": selected})


FIELD_MOVE_IDS = (148, 15, 19, 70, 57, 249, 127, 100, 91, 208, 135, 230)
# include/constants/party_menu.h defines 18 ordinary cursor options (0..17); the field
# move option ids therefore begin at 18.  src/data/party_menu.h adds the sFieldMoves index.
CURSOR_OPTION_FIELD_MOVES = 18
PARTY_ACTION_LABELS = {
    0: ("SUMMARY", "open summary"),
    1: ("SWITCH", "switch party order"),
    2: ("CANCEL", "cancel"),
    3: ("ITEM", "use item"),
    4: ("GIVE", "choose an item to give"),
    5: ("TAKE", "take the held item"),
    6: ("MAIL", "read mail"),
    7: ("TAKE", "take the held mail"),
    8: ("READ", "read the held mail"),
    9: ("CANCEL", "cancel"),
    10: ("SHIFT", "switch the active Pokemon"),
    11: ("SEND OUT", "send out this Pokemon"),
    12: ("ENTER", "enter this Pokemon"),
    13: ("NO ENTRY", "remove this Pokemon from the entry"),
    14: ("STORE", "store this Pokemon"),
    15: ("REGISTER", "register this Pokemon"),
    16: ("TRADE", "trade this Pokemon"),
    17: ("TRADE", "trade this Pokemon"),
}

HELD_ITEM_ACTION_IDS = frozenset({4, 5, 9})
MAIL_ACTION_IDS = frozenset({7, 8, 9})


def decode_party_action(
    snapshot: StateSnapshot,
    tables: RomTables | None = None,
    *,
    port: MemoryPort | None = None,
    profile: RomProfile | None = None,
) -> MenuModel | PromptModel | None:
    """Decode the action list built by ``SetPartyMonFieldSelectionActions``."""
    ui = snapshot.ui
    menu = ui.menu.party if ui and ui.valid else None
    if menu is None or menu.active_hint is not True:
        return None
    action_ids = menu.action_ids
    if (
        not action_ids
        and port is not None
        and profile is not None
        and profile.has_symbol("sPartyMenuInternal")
    ):
        try:
            internal_ptr = port.read_u32(profile.symbol("sPartyMenuInternal").address)
            raw = port.read_range(internal_ptr, 0x18)
            count = raw[0x17]
            if count > 8:
                return None
            action_ids = tuple(raw[0x0F : 0x0F + count])
        except (MemoryReadError, ValueError, struct.error):
            return None
    if not action_ids:
        return None
    mon = next((member for member in snapshot.party if member.slot == menu.slot_id), None)
    if mon is None:
        return None
    entries: list[MenuEntry] = []
    for action in action_ids:
        if action >= CURSOR_OPTION_FIELD_MOVES:
            field_index = action - CURSOR_OPTION_FIELD_MOVES
            if not 0 <= field_index < len(FIELD_MOVE_IDS):
                return None
            move_id = FIELD_MOVE_IDS[field_index]
            if not any(move.move_id == move_id for move in mon.moves):
                return None
            label = tables.move_name(move_id) if tables else None
            label = label or f"MOVE_{move_id}"
            entries.append(
                MenuEntry(
                    stable_key=f"party_action:field_move:{move_id}",
                    label=label,
                    effect=f"use field move {move_id}",
                    target_screen={
                        "scene": "field.free",
                        "callback": "CB2_Overworld",
                        "family": "field",
                    },
                )
            )
        else:
            label, effect = PARTY_ACTION_LABELS.get(action, (f"ACTION_{action}", "select action"))
            key = (
                "party_action:summary"
                if action == 0
                else "party_action:switch"
                if action == 1
                else "party_action:item"
                if action == 3
                else "party_action:mail"
                if action == 6
                else "party_action:cancel"
                if action in (2, 9)
                else f"party_action:{label.lower()}"
            )
            enabled = not (action == 5 and mon.held_item == 0)
            target = {
                "party_action:summary": {
                    "scene": "party.summary",
                    "callback": "CB2_SetUpPSS",
                    "recovery_only": True,
                },
                "party_action:switch": {
                    "scene": "party.list",
                    "callback": "CB2_UpdatePartyMenu",
                    "family": "start_party",
                },
                "party_action:item": {
                    "scene": "bag.items",
                    "callback": "CB2_BagMenuRun",
                    "family": "field",
                },
            }.get(key, {})
            entries.append(
                MenuEntry(
                    stable_key=key,
                    label=label,
                    effect=effect,
                    enabled=enabled,
                    target_screen=target,
                )
            )
    s_menu = getattr(ui.menu, "menu", None)
    cursor = s_menu.cursor_pos if s_menu and s_menu.window_in_use else None
    selected_key = (
        entries[cursor].stable_key if cursor is not None and 0 <= cursor < len(entries) else None
    )
    ids = frozenset(action_ids)
    surface = (
        "party.held_item"
        if ids <= HELD_ITEM_ACTION_IDS
        else "party.mail"
        if ids <= MAIL_ACTION_IDS
        else "party.action"
    )
    return MenuModel(
        surface=surface,
        entries=tuple(entries),
        selected_key=selected_key,
        scroll=0,
        cancel=True,
    )


def decode_bag_menu(snapshot: StateSnapshot, tables: RomTables) -> MenuModel | None:
    ui = snapshot.ui
    menu = ui.menu.bag if ui and ui.valid else None
    if menu is None or menu.active_hint is not True or not snapshot.health.valid:
        return None
    pocket_names = {p.pocket: p for p in snapshot.bag}
    pocket = ("items", "key_items", "poke_balls", "tms_hms", "berries")
    name = pocket[menu.pocket] if 0 <= menu.pocket < len(pocket) else None
    selected_pocket = pocket_names.get(name) if name else None
    if selected_pocket is None:
        return None
    entries_list: list[MenuEntry] = []
    for item in selected_pocket.items:
        label = tables.item_name(item.item_id)
        if label is None:
            return None
        entries_list.append(
            MenuEntry(
                stable_key=f"bag:{selected_pocket.pocket}:{item.item_id}",
                label=label,
                effect=f"use item {item.item_id}",
                enabled=item.quantity > 0,
            )
        )
    entries = tuple(entries_list)
    if not entries:
        return None
    cursor = menu.cursor_pos[menu.pocket] if 0 <= menu.pocket < len(menu.cursor_pos) else 0
    scroll = menu.items_above[menu.pocket] if 0 <= menu.pocket < len(menu.items_above) else 0
    return MenuModel(
        surface="bag.items",
        entries=entries,
        selected_key=entries[cursor].stable_key if 0 <= cursor < len(entries) else None,
        scroll=scroll,
        cancel=True,
    )


def decode_menu(
    snapshot: StateSnapshot,
    port: MemoryPort,
    profile: RomProfile,
    *,
    scene_id: str = "",
    prompt_text: DialogText | None = None,
) -> MenuModel | None:
    """Return a decoded model for the currently recognized target surface."""
    if snapshot.ui is None or not snapshot.ui.valid:
        return None
    from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver

    resolver = FireRedMenuResolver(port, profile)

    def resolve_entries(current, menu_id, count):
        entries = resolver.multichoice(current, menu_id, count)
        return tuple((entry.stable_key, entry.label) for entry in entries) if entries else None

    prompt = build_prompt_model(
        snapshot, prompt_text, scene_id=scene_id, menu_entries=resolve_entries
    )
    if prompt is not None:
        return prompt
    if scene_id in ("dialog.elevator", "menu.elevator"):
        return decode_elevator_menu(snapshot, port, scene_id=scene_id)
    if scene_id == "dialog.multichoice":
        return decode_multichoice_menu(snapshot, port, profile, surface=scene_id)
    if scene_id.startswith("shop."):
        return decode_shop_menu(snapshot, port, profile, scene_id=scene_id)
    if scene_id.startswith("menu.start"):
        return decode_start_menu(snapshot)
    tables = RomTables(port, profile, use_generated=True)
    if scene_id.startswith("party."):
        if scene_id in ("party.action", "party.held_item", "party.mail"):
            return decode_party_action(snapshot, tables, port=port, profile=profile)
        if scene_id == "party.reorder":
            return decode_party_reorder(snapshot, tables)
        return decode_party_menu(snapshot, tables)
    if scene_id.startswith("bag."):
        return decode_bag_menu(snapshot, tables)
    if scene_id.startswith(("tm.", "move.")):
        info = snapshot.ui.menu.list_menu
        if info is not None and info.window_in_use:
            is_tm = scene_id.startswith("tm.")
            return decode_list_menu(
                port,
                list_menu=info,
                surface="tm.list" if is_tm else "move.list",
                fallback_name=tables.item_name if is_tm else tables.move_name,
            )
    return None
