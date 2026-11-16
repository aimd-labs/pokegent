"""Shared matching for executable coordinate triggers and planned graph gates."""

from __future__ import annotations

from collections.abc import Iterable

_WALK_KINDS = frozenset({"walk", "bike", "forced", "ledge"})


def executable_route(
    route: Iterable,
    *,
    player_xy: tuple[int, int] | None = None,
    map_key: tuple[int, int] | None = None,
    surfing: bool = False,
) -> tuple:
    """The planned route minus steps the live RAM state has already performed.

    * A coordinate gate on the player's own tile has fired already (ROM coordinate events
      run on step-on), so it cannot be executed again without leaving the tile.
    * While the player is surfing, leading same-map ``surf`` edges (getting onto the water)
      are already done.  A ``surf`` edge that crosses into another map is a water connection
      (the player swims over the map seam) and stays a real step.
    """
    edges = list(route)
    if edges and edges[0].kind == "gate" and player_xy is not None:
        at = edges[0].target_dict().get("at")
        if (
            isinstance(at, (tuple, list))
            and tuple(at) == tuple(player_xy)
            and (map_key is None or edges[0].source.map_key == map_key)
        ):
            edges = edges[1:]
    if surfing:
        lead = 0
        while lead < len(edges) and edges[lead].kind in _WALK_KINDS | {"surf"}:
            lead += 1
        edges = [
            edge
            for edge in edges[:lead]
            if edge.kind != "surf" or edge.source.map_key != edge.destination.map_key
        ] + edges[lead:]
    return tuple(edges)


def first_planned_gate_at(
    route: Iterable,
    at: tuple[int, int],
    map_key: tuple[int, int] | None = None,
    *,
    player_xy: tuple[int, int] | None = None,
    surfing: bool = False,
) -> bool:
    """Whether the first executable non-walk route edge is an available gate at ``at``."""
    route = executable_route(route, player_xy=player_xy, map_key=map_key, surfing=surfing)
    first = next((edge for edge in route if edge.kind not in _WALK_KINDS), None)
    if (
        first is None
        or first.kind != "gate"
        or first.unmet
        or (map_key is not None and first.source.map_key != map_key)
    ):
        return False
    gate_at = first.target_dict().get("at")
    return isinstance(gate_at, (tuple, list)) and tuple(gate_at) == at
