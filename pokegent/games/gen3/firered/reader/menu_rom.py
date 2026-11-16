"""ROM-backed identities for FireRed cursor menus.

The live cursor and multichoice id come from RAM.  Labels and stable identities come from the
ROM tables/symbols which created those rows; screen pixels and row-number lookup tables are not
used.  A count mismatch fails closed so a cursor can never be assigned to the wrong entry.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass

from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, MemoryReadError
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

MULTICHOICE_NONE = 0xFF
MULTICHOICE_ROW_SIZE = 8  # struct MultichoiceListStruct: list pointer, count, padding
MENU_ACTION_SIZE = 8  # struct MenuAction: text pointer, callback pointer
MAIN_MENU_TEXT_SIZE = 8  # struct StorageMainMenuText: text pointer, description pointer
MAX_MENU_LABEL = 64


@dataclass(frozen=True)
class RomMenuEntry:
    stable_key: str
    label: str


class FireRedMenuResolver:
    """Resolve only menu structures proven by the active ROM profile."""

    def __init__(self, port: MemoryPort, profile: RomProfile) -> None:
        self._port = port
        self._profile = profile

    def _address(self, name: str) -> int | None:
        entry = self._profile.symbols.get(name)
        return entry.address if entry is not None else None

    def _text_at(self, address: int) -> str | None:
        raw = bytearray()
        try:
            for offset in range(MAX_MENU_LABEL):
                value = self._port.read_u8(address + offset)
                raw.append(value)
                if value == 0xFF:
                    return decode_gen3_text(bytes(raw)) or None
        except (MemoryReadError, UnicodeError, ValueError):
            return None
        return None

    def _symbol_text(self, name: str) -> str | None:
        address = self._address(name)
        return self._text_at(address) if address is not None else None

    def _entries_from_pointer_table(
        self,
        *,
        table_symbol: str,
        count: int,
        row_size: int,
        identities: Mapping[int, str],
    ) -> tuple[RomMenuEntry, ...] | None:
        table = self._address(table_symbol)
        if table is None or not 0 < count <= 16:
            return None
        entries: list[RomMenuEntry] = []
        try:
            raw = self._port.read_range(table, count * row_size)
            for index in range(count):
                text_pointer = struct.unpack_from("<I", raw, index * row_size)[0]
                label = self._text_at(text_pointer)
                stable_key = identities.get(text_pointer)
                if label is None or stable_key is None:
                    return None
                entries.append(RomMenuEntry(stable_key, label))
        except (MemoryReadError, struct.error, ValueError):
            return None
        return tuple(entries)

    def _identities(self, symbols: Mapping[str, str]) -> dict[int, str]:
        return {
            address: stable_key
            for symbol, stable_key in symbols.items()
            if (address := self._address(symbol)) is not None
        }

    def multichoice(
        self, snapshot, multichoice_id: int, count: int
    ) -> tuple[RomMenuEntry, ...] | None:
        """Resolve the RAM-selected multichoice list and independently verify its row count."""
        if multichoice_id == MULTICHOICE_NONE:
            return self._pc_entries(snapshot, count)
        table = self._address("sMultichoiceLists")
        if table is None or not 0 <= multichoice_id < 65 or not 0 < count <= 16:
            return None
        try:
            row = self._port.read_range(
                table + multichoice_id * MULTICHOICE_ROW_SIZE, MULTICHOICE_ROW_SIZE
            )
            actions, rom_count = struct.unpack_from("<IB", row)
            if rom_count != count:
                return None
            raw = self._port.read_range(actions, count * MENU_ACTION_SIZE)
            entries = []
            for index in range(count):
                pointer = struct.unpack_from("<I", raw, index * MENU_ACTION_SIZE)[0]
                label = self._text_at(pointer)
                if label is None:
                    return None
                entries.append(RomMenuEntry(f"choice:{index}", label))
            return tuple(entries)
        except (MemoryReadError, struct.error, ValueError):
            return None

    def _pc_entries(self, snapshot, count: int) -> tuple[RomMenuEntry, ...] | None:
        flags = getattr(snapshot, "flags", None)
        player = getattr(snapshot, "player", None)
        if flags is None or player is None:
            return None
        owner_symbol, owner_key = (
            ("gText_BillSPc", "pc:bill")
            if flags.is_set(C.FLAG_SYS_NOT_SOMEONES_PC)
            else ("gText_SomeoneSPc", "pc:someone")
        )
        owner = self._symbol_text(owner_symbol)
        player_template = self._address("gText_SPc")
        log_off = self._symbol_text("gText_LogOff")
        if owner is None or player_template is None or log_off is None:
            return None
        # gText_SPc begins with the two-byte {PLAYER} placeholder (FD 01).  Decode the suffix from
        # ROM, then apply the live player name exactly as StringExpandPlaceholders does in pret.
        suffix = self._text_at(player_template + 2)
        if suffix is None:
            return None
        entries = [
            RomMenuEntry(owner_key, owner),
            RomMenuEntry("pc:player", f"{player.name}{suffix}"),
        ]
        game_clear = flags.is_set(C.FLAG_SYS_GAME_CLEAR)
        if game_clear or flags.is_set(C.FLAG_SYS_POKEDEX_GET):
            oak = self._symbol_text("gText_ProfOakSPc")
            if oak is None:
                return None
            entries.append(RomMenuEntry("pc:oak", oak))
        if game_clear:
            hall = self._symbol_text("gText_HallOfFame_2")
            if hall is None:
                return None
            entries.append(RomMenuEntry("pc:hall_of_fame", hall))
        entries.append(RomMenuEntry("pc:log_off", log_off))
        return tuple(entries) if len(entries) == count else None

    def pokemon_storage(self, count: int) -> tuple[RomMenuEntry, ...] | None:
        """Resolve the currently displayed Pokémon-storage main menu by text-pointer identity."""
        identities = self._identities(
            {
                "gText_WithdrawPokemon": "pc_storage:withdraw",
                "gText_DepositPokemon": "pc_storage:deposit",
                "gText_MovePokemon": "pc_storage:move",
                "gText_MoveItems": "pc_storage:move_items",
                "gText_SeeYa": "pc_storage:see_ya",
            }
        )
        return self._entries_from_pointer_table(
            table_symbol="sMainMenuTexts",
            count=count,
            row_size=MAIN_MENU_TEXT_SIZE,
            identities=identities,
        )

    def player_pc_top(self, count: int) -> tuple[RomMenuEntry, ...] | None:
        identities = self._identities(
            {
                "gText_ItemStorage": "player_pc:item_storage",
                "gText_Mailbox": "player_pc:mailbox",
                "gText_TurnOff": "player_pc:cancel",
            }
        )
        return self._entries_from_pointer_table(
            table_symbol="sMenuActions_TopMenu",
            count=count,
            row_size=MENU_ACTION_SIZE,
            identities=identities,
        )

    def player_pc_items(self, count: int) -> tuple[RomMenuEntry, ...] | None:
        identities = self._identities(
            {
                "gText_WithdrawItem2": "player_pc:withdraw_item",
                "gText_DepositItem2": "player_pc:deposit_item",
                "gFameCheckerText_Cancel": "player_pc:item_cancel",
            }
        )
        return self._entries_from_pointer_table(
            table_symbol="sMenuActions_ItemPc",
            count=count,
            row_size=MENU_ACTION_SIZE,
            identities=identities,
        )

    def pc_item_list(self, snapshot):
        """Decode the allocated Item-PC ListMenu with stable item identities from RAM."""
        ui = getattr(snapshot, "ui", None)
        list_menu = ui.menu.list_menu if ui is not None and ui.valid else None
        storage = getattr(snapshot, "storage", None)
        if list_menu is None or storage is None:
            return None
        from pokegent.games.gen3.firered.reader.menu_decoders import decode_list_menu

        keys = tuple(f"pc_item:{item.item_id}" for item in storage.pc_items) + ("pc_item:cancel",)
        return decode_list_menu(
            self._port,
            list_menu=list_menu,
            surface="player.pc.item_list",
            stable_keys=keys,
            disabled_item_ids=frozenset(),
        )

    def bag(self, snapshot):
        """Use the generic Bag adapter with names read from this same ROM."""
        from pokegent.actions.battle.romtables import RomTables
        from pokegent.games.gen3.firered.reader.menu_decoders import decode_bag_menu

        return decode_bag_menu(snapshot, RomTables(self._port, self._profile, use_generated=True))
