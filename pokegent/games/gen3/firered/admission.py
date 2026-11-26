"""Game-owned fresh objective observation services (#139).

The objective store only needs two optional admission facts: a fresh core snapshot for
already-satisfied REACH and the derived shop observation for SHOP. Both come from one lazily
created :class:`LiveReader` over the same actor/profile, never from a cached decision source.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.rom_data import RomProfile


@dataclass(frozen=True, slots=True)
class ObjectiveObservationServices:
    fresh_snapshot: Callable[[], Any] | None
    shop_observation: Callable[[], Any] | None


def make_objective_observation_services(
    actor: Any,
    profile: RomProfile | None,
    run_id: str,
) -> ObjectiveObservationServices:
    """Build the two optional providers; an absent actor/profile constructs no reader."""
    if actor is None or profile is None:
        return ObjectiveObservationServices(None, None)
    from pokegent.games.gen3.firered.reader.live import LiveReader
    from pokegent.goals.shop_admission import ShopObservation

    observation_reader = LiveReader(actor, profile, run_id=run_id)

    def fresh_snapshot() -> Any | None:
        """Fresh core-only read from the same actor; never a cached decision snapshot."""
        try:
            snapshot = observation_reader.read().snapshot
        except Exception:  # an optional admission fact read stays UNKNOWN
            return None
        if not snapshot.usable or snapshot.rom_profile != profile.profile_id:
            return None
        return snapshot

    def shop_observation() -> ShopObservation | None:
        snapshot = fresh_snapshot()
        if snapshot is None:
            return None
        return ShopObservation.from_snapshot(snapshot, rom_sha1=profile.rom.sha1)

    return ObjectiveObservationServices(fresh_snapshot, shop_observation)
