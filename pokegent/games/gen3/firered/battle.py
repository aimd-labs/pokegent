"""FireRed-owned battle fact provider (#139).

The app wrapper delegates here so the game owns its battle facts factory. Basic facts and a
missing ROM path are answered before any mechanics import or profile access; everything else
forwards to the existing ``MechanicsFacts`` unchanged.
"""

from __future__ import annotations

import os

from pokegent.games.gen3.firered.rom.rom_data import RomProfile


def make_battle_facts(
    profile: RomProfile,
    rom_path: str | os.PathLike[str] | None,
    kind: str,
    *,
    opponent_knowledge: str = "guide",
):
    """The battle family's fact provider: Gen-3 mechanics from the ROM (``None`` = BasicFacts)."""
    if kind == "basic" or rom_path is None:
        return None
    from pokegent.games.gen3.adapters.battle_facts import MechanicsFacts

    return MechanicsFacts.from_rom_file(
        rom_path,
        profile_id=profile.profile_id,
        opponent_knowledge=opponent_knowledge,
    )
