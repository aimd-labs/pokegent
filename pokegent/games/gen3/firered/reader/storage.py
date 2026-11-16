"""Pure decoder for the FireRed ``PokemonStorage`` save payload.

The storage object is kept outside SaveBlock1/2 in the save-section buffer on
this ROM.  This module deliberately takes bytes supplied by a verified reader;
it does not guess a RAM address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pokegent.games.gen3.firered.reader.memory_port import MemoryPort
from pokegent.games.gen3.firered.reader.mon import (
    MonDecodeError,
    decode_box_mon,
    is_empty_slot,
)
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

BOX_COUNT = 14
BOX_CAPACITY = 30
BOX_NAME_LENGTH = 9
# ``currentBox`` is followed by three alignment bytes before BoxPokemon[0].
STORAGE_HEADER_SIZE = 4
STORAGE_BOXES_SIZE = BOX_COUNT * BOX_CAPACITY * C.BOX_MON_SIZE
STORAGE_NAMES_SIZE = BOX_COUNT * BOX_NAME_LENGTH
STORAGE_SIZE = STORAGE_HEADER_SIZE + STORAGE_BOXES_SIZE + STORAGE_NAMES_SIZE + BOX_COUNT


@dataclass(frozen=True)
class PcItem:
    item_id: int
    quantity: int


SAVE_BLOCK1_SIZE = 0x3D68
SAVE_BLOCK_MOVE_RANGE = 128
# The linker places storage after the 128-byte DMA buffer (measured from the
# real Lv71 state); the four-byte alignment is inside PokemonStorage.
STORAGE_FROM_SAVE_BLOCK1 = SAVE_BLOCK1_SIZE + SAVE_BLOCK_MOVE_RANGE

# PokemonStorageSystemData offsets from the verified FireRed decompile. These are read only when
# the profile exposes gStorage; they let macros re-decode the identity under the cursor after every
# navigation input instead of assuming a key-press count.
G_STORAGE_BOX_OPTION_OFFSET = 0x01
# PokemonStorageSystemData.chooseBoxMenu.curBox: the deposit popup cursor,
# distinct from PokemonStorage.current_box.
G_STORAGE_DEPOSIT_BOX_CURSOR_OFFSET = 0x209C
# PokemonStorageSystemData.movingMon is the live carried Pokemon. The similarly named static
# sMonBeingCarried is only a scratch copy used while leaving/reopening the storage screen.
G_STORAGE_MOVING_MON_OFFSET = 0x20A0
# Static cursor/move symbols in the supported BPRE Rev0 ROM. Unlike gStorage's requested
# ``newCursor*`` fields, the cursor pair changes only when DoCursorNewPosUpdate completes sprite
# movement. The following bytes are the adjacent statics declared by pokemon_storage_system_data.c.
S_STORAGE_CURSOR_AREA_ADDRESS = 0x0203_9820
S_STORAGE_CURSOR_POSITION_ADDRESS = 0x0203_9821
S_STORAGE_IS_MON_BEING_MOVED_ADDRESS = 0x0203_9822
S_STORAGE_MOVING_MON_ORIG_BOX_ADDRESS = 0x0203_9823
S_STORAGE_MOVING_MON_ORIG_POSITION_ADDRESS = 0x0203_9824
S_STORAGE_MOVE_STATE_SIZE = 5


@dataclass(frozen=True)
class PokemonStorage:
    current_box: int
    boxes: tuple[tuple[object | None, ...], ...]
    names: tuple[str, ...]
    wallpapers: tuple[int, ...]
    # SaveBlock2.optionsButtonMode: 0=HELP, 1=LR, 2=L_EQUALS_A.
    options_button_mode: int = 0
    # Live UI fields are optional because save-block decoding also serves offline callers.
    ui_state: int | None = None  # gStorage->state; input is accepted only in state 0
    box_option: int | None = None  # 0 withdraw, 1 deposit, 2 move, 3 move items
    cursor_area: int | None = None  # 0 box, 1 party, 2 title, 3 buttons
    cursor_position: int | None = None
    deposit_box_cursor: int | None = None
    is_holding_mon: bool | None = None
    held_mon: object | None = None
    held_origin_kind: Literal["box", "party"] | None = None
    held_origin_box: int | None = None
    held_origin_slot: int | None = None
    # SaveBlock1.pcItems quantities use GetPcItemQuantity's XOR-zero representation. Keeping them
    # in this sidecar avoids changing the public state/wire schema while making item-PC choices
    # and their postconditions observable from RAM.
    pc_items: tuple[PcItem, ...] = ()

    def get(self, box: int, slot: int) -> object | None:
        if not 0 <= box < BOX_COUNT or not 0 <= slot < BOX_CAPACITY:
            raise IndexError((box, slot))
        return self.boxes[box][slot]

    @property
    def occupied(self) -> int:
        return sum(mon is not None for box in self.boxes for mon in box)

    @property
    def cursor_settled(self) -> bool:
        # The static sCursor* pair is written by DoCursorNewPosUpdate only when movement finishes.
        return self.cursor_area is not None and self.cursor_position is not None


def read_storage(port: MemoryPort, save_block1_ptr: int) -> PokemonStorage:
    """Read storage beside the relocated SaveBlock1 from one atomic RAM view.

    ``SetSaveBlocksPointers`` relocates SaveBlock1 and PokemonStorage by the
    same 128-byte random offset; the fixed gap is from the Rev0 decompile.
    Callers must supply a pointer read in the same snapshot as this range.
    """
    raw = port.read_range(save_block1_ptr + STORAGE_FROM_SAVE_BLOCK1, STORAGE_SIZE)
    return decode_storage(raw)


def decode_storage(raw: bytes) -> PokemonStorage:
    """Decode one complete ``PokemonStorage`` object.

    Empty all-zero slots are represented as ``None``.  A non-empty slot must
    pass the same checksum/decryption validation as a party member; corrupt
    data raises instead of becoming an option-visible Pokemon.
    """
    if len(raw) != STORAGE_SIZE:
        raise ValueError(f"PokemonStorage is {STORAGE_SIZE} bytes, got {len(raw)}")
    current_box = raw[0]
    if current_box >= BOX_COUNT:
        raise ValueError(f"current box {current_box} outside 0..{BOX_COUNT - 1}")
    boxes: list[tuple[object | None, ...]] = []
    offset = STORAGE_HEADER_SIZE
    for box in range(BOX_COUNT):
        slots: list[object | None] = []
        for slot in range(BOX_CAPACITY):
            chunk = raw[offset : offset + C.BOX_MON_SIZE]
            offset += C.BOX_MON_SIZE
            if is_empty_slot(chunk):
                slots.append(None)
                continue
            try:
                slots.append(decode_box_mon(chunk))
            except MonDecodeError as exc:
                raise ValueError(f"box {box} slot {slot}: {exc}") from exc
        boxes.append(tuple(slots))
    names = tuple(
        decode_gen3_text(raw[offset + i * BOX_NAME_LENGTH : offset + (i + 1) * BOX_NAME_LENGTH])
        for i in range(BOX_COUNT)
    )
    offset += STORAGE_NAMES_SIZE
    return PokemonStorage(
        current_box=current_box,
        boxes=tuple(boxes),
        names=names,
        wallpapers=tuple(raw[offset : offset + BOX_COUNT]),
    )
