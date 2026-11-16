"""Battle RAM symbols and ROM tables: the names this family needs, resolved through the PROFILE.

Until M2.5 the 20 battle symbols (battler structs, battle script engine state, enemy party ...)
lived here with their provenance because ``references/rom_profiles.yaml`` belonged to another work
package. The integrator moved them, unchanged, into the profile (``symbols`` section, each with
``source`` and ``verified_by``); the ROM name/move/item tables were already in its ``rom_tables``
section (mechanics WP). This module now only

* lists the names the family reads (:data:`REQUIRED_SYMBOLS`, :data:`ROM_TABLE_LAYOUT`), so a
  profile that lacks one is detectable, and
* resolves them (:func:`resolve`, :func:`rom_table`): never a guessed address, ``None`` when the
  profile does not carry the symbol (the battle block is then ``valid=False`` and every battle
  builder reports UNKNOWN).

Provenance of every address: ``RomProfile.symbol(name).source`` / ``.verified_by`` and
``RomProfile.rom_table(name)``; method notes in
``the runtime contracts`` section 4.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.gen3.firered.rom.rom_data import RomProfile

#: RAM symbols the battle reader needs besides the UI symbols (``gBattleTypeFlags`` ...)
REQUIRED_SYMBOLS: tuple[str, ...] = (
    "gBattleMons",
    "gBattlescriptCurrInstr",
    "gLastUsedItem",
    "gChosenActionByBattler",
    "gLastMoves",
    "gChosenMoveByBattler",
    "gStatuses3",
    "gDisableStructs",
    "gRandomTurnNumber",
    "gBattleCommunication",
    "gBattleOutcome",
    "gBattleWeather",
    "gBattleScripting",
    "gBattleStruct",
    "gDisplayedStringBattle",
    "gMoveToLearn",
    "gEnemyParty",
    "gBattleMainFunc",
    "gBattleResults",
    "gNumberOfMovesToChoose",
)

#: symbols the reader uses when the profile has them (the block degrades to "unknown" otherwise)
OPTIONAL_SYMBOLS: tuple[str, ...] = (
    "gSideStatuses",
    "gSideTimers",
    "sPokedexScreenData",
    "gTrainerBattleOpponent_A",
    "gNumSafariBalls",
)

#: row stride and entry count of the ROM tables the family reads (decomp struct layouts: the
#: structural half of a table; the ADDRESS comes from the profile's ``rom_tables``)
ROM_TABLE_LAYOUT: dict[str, tuple[int, int]] = {
    "gMoveNames": (13, 355),  # MOVE_NAME_LENGTH 12 + 1, MOVES_COUNT
    "gSpeciesNames": (11, 412),  # POKEMON_NAME_LENGTH 10 + 1
    "gAbilityNames": (13, 78),  # ABILITY_NAME_LENGTH 12 + 1, ABILITIES_COUNT
    "gItems": (44, 375),  # struct Item (include/item.h), ITEMS_COUNT
    "gItemEffectTable": (4, 163),  # u8 *[]: ITEM_POTION (13) .. ITEM_ENIGMA_BERRY (175)
    "gBattleMoves": (12, 355),  # struct BattleMove padded to 12
    "gSpeciesInfo": (28, 412),  # struct SpeciesInfo; types are bytes 6 and 7
}


@dataclass(frozen=True)
class RomTable:
    name: str
    address: int
    stride: int
    count: int
    source: str
    verified_by: tuple[str, ...] = ()


def resolve(profile: RomProfile, name: str) -> int | None:
    """Address of the profile symbol ``name``, or ``None`` if the profile does not carry it."""
    if profile.has_symbol(name):
        return profile.symbol(name).address
    return None


def missing_symbols(profile: RomProfile) -> tuple[str, ...]:
    """Battle symbols and ROM tables this profile lacks (empty for Rev0)."""
    gone = [n for n in REQUIRED_SYMBOLS if not profile.has_symbol(n)]
    gone += [n for n in ROM_TABLE_LAYOUT if not profile.has_rom_table(n)]
    return tuple(gone)


def rom_table(profile: RomProfile, name: str) -> RomTable | None:
    layout = ROM_TABLE_LAYOUT.get(name)
    if layout is None or not profile.has_rom_table(name):
        return None
    entry = profile.rom_table(name)
    stride, count = layout
    return RomTable(name, entry.address, stride, count, entry.source, entry.verified_by)
