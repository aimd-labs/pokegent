#!/usr/bin/env python3
"""Print one offline raw mGBA RAM snapshot as JSON or a compact numeric summary.

Source-checkout script: --ram-image FILE [--summary] [--party-source saveblock].
Exit 0: usable snapshot; 1: invalid RAM (health retained); 2: input/profile/usage error.
Reads only the explicit state and profile metadata; creates no output files.
Adapted from tools/snapshot_dump.py, source A at
7c8742ba50a7a482e24d69bb1364c49e63c4e453. MIT; see LICENSE and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokegent.games.gen3.firered.reader.reader import FireRedReader  # noqa: E402
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot  # noqa: E402
from pokegent.games.gen3.firered.rom.conformance import (  # noqa: E402
    StateFormatError,
    load_mgba_state_ram,
)
from pokegent.games.gen3.firered.rom.rom_data import ProfileError, load_profiles  # noqa: E402


def summarize(snapshot: StateSnapshot, meta: dict) -> str:
    lines = [f"source: {meta['source']} file={meta['file']}"]
    lines.append(f"health: {'VALID' if snapshot.usable else 'INVALID'} hash={snapshot.state_hash}")
    for error in snapshot.health.read_errors:
        lines.append(f"  ! {error.subsystem}/{error.code}: {error.message}")
    if not snapshot.usable:
        return "\n".join(lines)
    player = snapshot.player
    assert player is not None
    lines.append(
        f"player: map {player.map_group}.{player.map_num} pos ({player.x},{player.y}) "
        f"facing {player.facing} money {player.money} coins {player.coins} "
        f"badges {list(player.badges)}"
    )
    if not snapshot.party:
        lines.append("party: empty")
    for mon in snapshot.party:
        moves = ",".join(str(move.move_id) for move in mon.moves if move.move_id)
        lines.append(
            f"  party[{mon.slot}]: species {mon.species} L{mon.level} "
            f"HP {mon.hp}/{mon.max_hp} moves {moves}{' EGG' if mon.is_egg else ''}"
        )
    for pocket in snapshot.bag:
        if pocket.items:
            items = " ".join(f"{item.item_id}x{item.quantity}" for item in pocket.items)
            lines.append(f"bag {pocket.pocket}: {items}")
    assert snapshot.flags is not None and snapshot.vars is not None
    lines.append(
        f"flags set: {len(snapshot.flags.set_ids)} vars non-zero: {len(snapshot.vars.nonzero)}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ram-image", type=Path, required=True)
    parser.add_argument("--profile", default="firered-us-rev0")
    parser.add_argument("--party-source", choices=("live", "saveblock"), default="live")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        profile = load_profiles().get(args.profile)
        if not args.ram_image.is_file():
            raise OSError("RAM image must be a readable file")
        port = load_mgba_state_ram(args.ram_image)
        snapshot = FireRedReader(port, profile, party_source=args.party_source).read_snapshot()
    except (OSError, StateFormatError, ProfileError, ValueError, KeyError,
            TypeError, AttributeError, struct.error, IndexError) as exc:
        # Profile parsing can also reject malformed YAML shapes/field layouts.
        message = " ".join(str(exc).split())[:400]
        print(f"error: {message}", file=sys.stderr)
        return 2
    meta = {"source": "ram-image", "file": args.ram_image.name}
    if args.summary:
        print(summarize(snapshot, meta))
    else:
        print(json.dumps({"meta": meta, "snapshot": snapshot.model_dump(mode="json")}))
    return 0 if snapshot.usable else 1


if __name__ == "__main__":
    sys.exit(main())
