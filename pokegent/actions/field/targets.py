"""Targets of the current map and the plan to each of them.

A *target* is something the player can go to and do something about: a warp, a connection edge, an
NPC, a sign, an item ball, a coordinate the active objective names. It carries the standing tiles
(``approaches``) from which the final action works and the kind of final action:

=========  ============================================================================
final      what happens at the end of the walk
=========  ============================================================================
``step``   walk onto the warp tile (mat, ladder, cave door, stairs of the "step" kind)
``door``   stand south of a warp door, face north, press north
``hold``   stand on an arrow/stairs warp tile and hold its direction
``edge``   step off the map edge into the connection ring
``talk``   face the object, press A (NPC, sign, item ball)
``none``   arrive (coordinate target or grass patch)
=========  ============================================================================

Ids are stable and semantic (``warp:<group>:<number>:<idx>``, ``edge:<dir>:<group>:<number>``,
``talk:<local_id>``, ``sign:<bg_index>``, ``item:<local_id>``, ``tile:<x>:<y>``) and never contain a
position of something that moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pokegent.actions.field import nav as N
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_schema import Facing, MapObject, MapState
from pokegent.games.gen3.firered.maps.obstacles import ROLES, from_object

# -- naming ---------------------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z0-9])")
_FLOOR = re.compile(r"B?\d+F|\d+")


def pretty_map_name(name: str | tuple[int, int] | None) -> str:
    """``PewterCity_PokemonCenter_1F`` -> ``Pewter City, Pokemon Center 1F``."""
    if isinstance(name, tuple):
        from pokegent.games.gen3.firered.maps.map_names import map_name

        name = map_name(*name)
    if not name:
        return "an unknown place"
    parts = [_CAMEL.sub(" ", part) for part in name.split("_") if part]
    if len(parts) > 1 and _FLOOR.fullmatch(parts[-1]):
        last = parts.pop()
        parts[-1] += f" {last}"
    return ", ".join(parts)


# -- model ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Approach:
    stand: tuple[int, int]
    face: Facing | None = None  # facing needed at the end (None: any)


@dataclass(frozen=True)
class Target:
    option_id: str
    kind: str  # warp | edge | npc | sign | item | obstacle | grass | tile
    final: str  # step | door | hold | edge | talk | none
    label: str
    approaches: tuple[Approach, ...] = ()
    #: a ring tile direction for ``edge`` targets
    edge: str | None = None
    #: warp tile / object tile / sign tile (the thing itself)
    at: tuple[int, int] | None = None
    #: arrow/stairs: the direction held on the warp tile; door: "N"
    press: Facing | None = None
    unsupported: str | None = None  # a typed reason: recognised but not acted on
    unavailable: str | None = None  # a typed reason: the game's rules make it pointless
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    target: Target
    moves: tuple[N.Move, ...]
    approach: Approach
    cost: int
    #: the facing a final turn tap must produce (None: already facing it or any facing is fine)
    needs_turn: Facing | None

    @property
    def length(self) -> int:
        return len(self.moves)

    @property
    def grass_tiles(self) -> int:
        return sum(1 for m in self.moves if m.grass)

    @property
    def encounter_tiles(self) -> int:
        return sum(m.encounter_tiles for m in self.moves)

    @property
    def ledges(self) -> int:
        return sum(1 for m in self.moves if m.kind == "ledge")

    @property
    def script_tiles(self) -> int:
        return sum(1 for m in self.moves if m.script)

    @property
    def forced_tiles(self) -> int:
        return sum(len(m.forced_tiles) for m in self.moves)

    @property
    def forced_landing(self) -> tuple[int, int] | None:
        for move in reversed(self.moves):
            if move.kind == "forced":
                return move.dst
        return None


# -- enumeration ----------------------------------------------------------------------------------


def _object_label(obj: MapObject) -> str:
    if obj.graphics_id == K.OBJ_EVENT_GFX_NURSE:
        return "the nurse"
    if obj.graphics_id == K.OBJ_EVENT_GFX_CLERK:
        return "the shop clerk"
    if obj.role == "trainer" or obj.trainer_type in (1, 2):
        return "a trainer"
    if obj.role == "item_ball":
        return "an item on the ground"
    if obj.role == "cut_tree":
        return "the Cut tree"
    if obj.role == "rock_smash":
        return "the smashable rock"
    if obj.role == "boulder":
        return "the Strength boulder"
    if obj.role == "snorlax":
        return "the sleeping Snorlax"
    if obj.role == "fossil":
        return "the fossil"
    return "a person"


def warp_target(state: MapState, nav: N.Navigator, warp) -> Target:
    ident = state.identity
    dest = pretty_map_name(warp.dest_name)
    option_id = f"warp:{ident.group}:{ident.number}:{warp.index}"
    trigger = warp.trigger or "step"
    info: dict[str, Any] = {
        "warp": warp.index,
        "at": [warp.x, warp.y],
        "dest": None if warp.dynamic else [warp.dest_group, warp.dest_number],
        "dest_warp": warp.dest_warp_id,
        "dest_name": dest,
        "trigger": trigger,
    }
    label = f"Go through the {_warp_noun(warp)} to {dest}"
    at = (warp.x, warp.y)
    if trigger == "door":
        stand = (warp.x, warp.y + 1)
        return Target(option_id, "warp", "door", label, (Approach(stand, "N"),), at=at,
                      press="N", info=info)  # fmt: skip
    if trigger in ("arrow", "stairs"):
        press = warp.trigger_direction
        # Directional warps are entered from the tile itself, but the ROM checks the
        # avatar's facing before it checks the held direction.  Make that facing part
        # of the approach contract so a final turn never becomes an accidental step
        # off the stair/arrow tile.
        return Target(option_id, "warp", "hold", label, (Approach(at, press),), at=at,
                      press=press, info=info)  # fmt: skip
    if trigger == "none":
        return Target(option_id, "warp", "step", label, (), at=at, info=info,
                      unsupported="script_driven_warp")  # fmt: skip
    if warp.trigger is None:
        # a warp event on a plain tile. Walking onto it did NOT warp live (the twins beside a
        # Pokemon Center / Mart exit mat: only the arrow warp next to them fires). Twins are inert;
        # a lone one (a cave hole, a league doorway) is offered as untested, verified when walked.
        twin = any(
            o.index != warp.index
            and o.trigger in ("door", "step", "arrow", "stairs")
            and (o.dest_group, o.dest_number) == (warp.dest_group, warp.dest_number)
            and max(abs(o.x - warp.x), abs(o.y - warp.y)) <= 2
            for o in state.warps
        )
        info["untested"] = not twin
        if twin:
            return Target(option_id, "warp", "step", label, (), at=at, info=info,
                          unavailable="inert_duplicate")  # fmt: skip
    return Target(option_id, "warp", "step", label, (Approach(at, None),), at=at, info=info)


def _warp_noun(warp) -> str:
    return {
        "door": "door",
        "arrow": "exit",
        "stairs": "stairs",
        "step": {"ladder": "ladder", "cave_door": "cave entrance", "hole": "hole"}.get(
            warp.tile_kind or "", "entrance"
        ),
    }.get(warp.trigger or "step", "entrance")


def edge_targets(state: MapState) -> list[Target]:
    out = []
    for conn in state.connections:
        if conn.direction not in ("N", "S", "W", "E"):
            continue  # dive / emerge
        dest = pretty_map_name(conn.dest_name)
        side = {"N": "north", "S": "south", "W": "west", "E": "east"}[conn.direction]
        out.append(
            Target(
                f"edge:{conn.direction}:{conn.dest_group}:{conn.dest_number}",
                "edge",
                "edge",
                f"Leave the area to the {side}, toward {dest}",
                (),
                edge=conn.direction,
                info={
                    "direction": conn.direction,
                    "dest": [conn.dest_group, conn.dest_number],
                    "dest_name": dest,
                },
            )
        )
    return out


def object_targets(
    state: MapState,
    nav: N.Navigator,
    *,
    required_npc: int | None = None,
    exempt_required_npc_hidden: bool = False,
) -> list[Target]:
    out = []
    for obj in state.objects:
        # A scripted objective may name a ROM object that has not been materialized in the
        # current runtime object slots yet (for example Oak is removed during the parcel return
        # cutscene until the player approaches him). Keep that target routable; fixed item balls
        # are also targets before they enter the camera spawn window because they block routes.
        # The executor's postcondition verifies that the script actually ran.
        if (
            (obj.live is None and obj.local_id != required_npc and obj.role != "item_ball")
            or (obj.live is not None and obj.live.invisible)
            or obj.script_ptr is None
        ):
            continue  # not spawned, hidden, or nothing to run: nothing to talk to
        # A navigation target may be retained outside the camera only after RAM proves its hide
        # flag clear. A set or unreadable flag means the ROM object has not spawned yet.
        # Item balls invert that: their flag records "already picked up", so a cleared or
        # unreadable flag must NOT hide the ball (an unknown flag on a ball is still findable).
        if obj.role == "item_ball":
            if obj.hidden_flag_set is True:  # ball already taken
                continue
        elif (
            obj.hidden_flag_id
            and obj.hidden_flag_set is not False
            and not (
                exempt_required_npc_hidden
                and obj.local_id == required_npc
                and obj.hidden_flag_set is None
            )
        ):
            continue
        if obj.role in N.OBSTACLE_ROLES or obj.role == "fossil":
            continue  # field-mechanic targets are emitted by obstacle_targets
        kind, prefix = ("item", "item") if obj.role == "item_ball" else ("npc", "talk")
        approaches = tuple(
            Approach(stand, face) for stand, face in nav.interaction_approaches((obj.x, obj.y))
        )
        trainer = obj.trainer_type in (1, 2)
        out.append(
            Target(
                f"{prefix}:{obj.local_id}",
                kind,
                "talk",
                f"Talk to {_object_label(obj)}" if kind == "npc" else "Pick up the item",
                approaches,
                at=(obj.x, obj.y),
                info={
                    "local_id": obj.local_id,
                    "objective_target": obj.local_id == required_npc,
                    "trainer": trainer,
                    "mobile": N.is_mobile(obj),
                    "role": obj.role,
                },
            )
        )
    return out


def obstacle_targets(state: MapState, nav: N.Navigator) -> list[Target]:
    """Field-mechanic targets whose final A press belongs to the field macro."""
    out: list[Target] = []
    for obj in state.objects:
        if obj.role not in ROLES | {"fossil"}:
            continue
        if obj.hidden_flag_set is True or not (obj.live is not None or obj.blocks_tile):
            continue
        approaches = tuple(
            Approach(stand, face) for stand, face in nav.interaction_approaches((obj.x, obj.y))
        )
        if not approaches:
            continue
        obstacle = from_object(obj)
        if obstacle is not None:
            option_id = obstacle.option_id
            verb = obstacle.kind.verb
            label = obstacle.kind.label
        else:
            option_id, verb, label = f"fossil:{obj.local_id}", "INTERACT_WITH", "Choose this fossil"
        out.append(
            Target(
                option_id,
                "obstacle",
                "talk",
                label,
                approaches,
                at=(obj.x, obj.y),
                info={
                    "local_id": obj.local_id,
                    "role": obj.role,
                    "verb": verb,
                    "map_name": state.identity.name,
                    **(
                        {
                            "requirement_key": obstacle.kind.requirement_key,
                            "requirement_kind": obstacle.kind.requirement_kind,
                            "completion": obstacle.kind.completion,
                            "prompt_key": obstacle.kind.prompt_key,
                            "hide_flag": obstacle.hide_flag,
                            "irreversible": obstacle.kind.irreversible,
                        }
                        if obstacle is not None
                        else {}
                    ),
                },
            )
        )
    return out


def grass_patches(state: MapState) -> list[tuple[tuple[int, int], ...]]:
    """Connected tall-grass components, in stable map-coordinate order."""
    grass = {
        (x, y)
        for y in range(state.layout.height)
        for x in range(state.layout.width)
        if state.tile(x, y).tall_grass
    }
    patches: list[tuple[tuple[int, int], ...]] = []
    while grass:
        start = min(grass)
        grass.remove(start)
        todo = [start]
        patch = [start]
        while todo:
            x, y = todo.pop()
            for nxt in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if nxt in grass:
                    grass.remove(nxt)
                    todo.append(nxt)
                    patch.append(nxt)
        patches.append(tuple(sorted(patch)))
    return patches


def grass_targets(state: MapState, nav: N.Navigator) -> list[Target]:
    out: list[Target] = []
    search = nav.search()
    for patch in grass_patches(state):
        if len(patch) < 2 or not any(search.best_at(*xy) is not None for xy in patch):
            continue
        first, last = patch[0], patch[-1]
        out.append(
            Target(
                f"grass:{first[0]}:{first[1]}",
                "grass",
                "none",
                "Train in this tall-grass patch",
                (Approach(first), Approach(last)),
                at=first,
                info={"patch": patch, "endpoints": (first, last), "map_name": state.identity.name},
            )
        )
    return out


def cave_training_targets(state: MapState, nav: N.Navigator) -> list[Target]:
    """Offer one nearby bounded run on non-grass, land-encounter tiles.

    FireRed's map-header ``cave`` flag means "requires Flash", not "is an underground map";
    Victory Road therefore reports ``cave == 0``.  The tile encounter attribute is the ROM-backed
    fact that matters.  Tall grass is left to :func:`grass_targets`.
    """
    floor = {
        (x, y)
        for y in range(state.layout.height)
        for x in range(state.layout.width)
        if state.tile(x, y).encounter == "land"
        and state.tile(x, y).walkable
        and not state.tile(x, y).tall_grass
    }
    search = nav.search()
    pairs: list[tuple[int, int, tuple[int, int], tuple[int, int]]] = []
    direction = {(1, 0): "E", (0, 1): "S"}
    reverse = {"E": "W", "S": "N"}
    for first in sorted(floor):
        node = search.best_at(*first)
        if node is None:
            continue
        for vector, facing in direction.items():
            last = first[0] + vector[0], first[1] + vector[1]
            if last not in floor:
                continue
            forward = nav.step(node, facing)
            if forward is None or forward[0][:2] != last:
                continue
            back = nav.step(forward[0], reverse[facing])
            if back is None or back[0][:2] != first:
                continue
            last_node = search.best_at(*last)
            if last_node is None:
                continue
            pairs.append((min(search.cost[node], search.cost[last_node]),
                          max(search.cost[node], search.cost[last_node]), first, last))  # fmt: skip
    if not pairs:
        return []
    _near, _far, first, last = min(pairs)
    return [
        Target(
            f"train:{first[0]}:{first[1]}",
            "grass",
            "none",
            "Train here on the encounter-enabled floor",
            (Approach(first), Approach(last)),
            at=first,
            info={
                "patch": (first, last),
                "endpoints": (first, last),
                "map_name": state.identity.name,
                "surface": "cave",
            },
        )
    ]


def sign_targets(state: MapState, nav: N.Navigator) -> list[Target]:
    out = []
    for bg in state.bg_events:
        if bg.kind != "sign":
            continue  # hidden items / secret bases are not offered (spec 7.1 discovery policy)
        approaches = tuple(
            Approach(stand, face)
            for stand, face in nav.interaction_approaches(
                (bg.x, bg.y), face_only=bg.sign_facing, counter_ok=False
            )
        )
        out.append(
            Target(
                f"sign:{bg.index}",
                "sign",
                "talk",
                "Read the sign",
                approaches,
                at=(bg.x, bg.y),
                info={"bg": bg.index, "script_ptr": bg.script_ptr},
            )
        )
    return out


def pc_targets(state: MapState, nav: N.Navigator) -> list[Target]:
    """Return ROM furniture tiles whose field script opens the Pokemon PC.

    Centers and the player's house expose the PC as a background/object script rather than a
    normal object event.  The metatile behavior is the stable ROM identity, so this deliberately
    scans the map grid instead of guessing a map name or coordinates.
    """
    out: list[Target] = []
    for y in range(state.layout.height):
        for x in range(state.layout.width):
            tile = state.tile(x, y)
            if tile.interactive != "pc":
                continue
            approaches = tuple(
                Approach(stand, face)
                for stand, face in nav.interaction_approaches((x, y), counter_ok=False)
            )
            if approaches:
                out.append(
                    Target(
                        f"use_pc:{x}:{y}",
                        "pc",
                        "talk",
                        "Use the PC",
                        approaches,
                        at=(x, y),
                        info={"x": x, "y": y, "role": "pc"},
                    )
                )
    return out


def tile_target(state: MapState, x: int, y: int) -> Target:
    return Target(
        f"tile:{x}:{y}",
        "tile",
        "none",
        f"Walk to a specific spot of this area ({x}, {y})",
        (Approach((x, y), None),),
        at=(x, y),
        info={"x": x, "y": y},
    )


def enumerate_targets(
    state: MapState,
    nav: N.Navigator,
    *,
    tiles: tuple[tuple[int, int], ...] = (),
    training: bool = False,
    required_npc: int | None = None,
    exempt_required_npc_hidden: bool = False,
) -> list[Target]:
    targets = [warp_target(state, nav, w) for w in state.warps]
    targets += edge_targets(state)
    targets += obstacle_targets(state, nav)
    targets += grass_targets(state, nav)
    if training:
        targets += cave_training_targets(state, nav)
    targets += object_targets(
        state, nav, required_npc=required_npc,
        exempt_required_npc_hidden=exempt_required_npc_hidden,
    )
    targets += pc_targets(state, nav)
    # Hidden items remain adapter facts, but their walk/face/dialog interaction is not
    # represented by the proven executable target contract yet.
    targets += sign_targets(state, nav)
    targets += [tile_target(state, x, y) for x, y in tiles]
    return targets


# -- planning -------------------------------------------------------------------------------------


def plan_target(nav: N.Navigator, search: N.Search, target: Target) -> Plan | None:
    """Cheapest way to the target on ``search``; ``None`` if no approach is reachable."""
    best: tuple[int, Plan] | None = None
    if target.final == "edge":
        for node, cost in search.cost.items():
            move = (search.parent.get(node) or (None, None))[1]
            if move is None or move.connection != target.edge:
                continue
            if target.at is not None and move.src != target.at:
                continue
            moves = search.path_to(node)
            plan = Plan(target, moves, Approach(move.dst, None), cost, None)
            if best is None or (cost, len(moves)) < (best[0], best[1].length):
                best = (cost, plan)
        return best[1] if best else None

    facing_now = nav.state.player.facing if nav.state.player else None
    for approach in target.approaches:
        node = search.best_at(*approach.stand)
        if node is None:
            continue
        if target.final == "step" and node == search.start:
            # standing ON the warp tile (a mat arrival): stepping on it warps, so step off and back
            replay = _step_off_and_back(nav, search.start)
            if replay is None:
                continue
            moves = replay
            cost = sum(m.cost for m in moves)
            plan = Plan(target, moves, approach, cost, None)
            if best is None or (cost, len(moves)) < (best[0], best[1].length):
                best = (cost, plan)
            continue
        if node in search.terminal and target.final not in ("step", "hold"):
            continue  # a warp tile on the way is a trap, not a standing place
        moves = search.path_to(node)
        last_dir = moves[-1].direction if moves else facing_now
        turn = None
        if approach.face is not None and last_dir != approach.face:
            turn = approach.face
        cost = search.cost[node] + (1 if turn else 0)
        plan = Plan(target, moves, approach, cost, turn)
        key = (cost, len(moves))
        if best is None or key < (best[0], best[1].length):
            best = (cost, plan)
    return best[1] if best else None


def _step_off_and_back(nav: N.Navigator, start: N.Node) -> tuple[N.Move, ...] | None:
    best: tuple[int, tuple[N.Move, ...]] | None = None
    for d in N.DIRECTIONS:
        out = nav.step(start, d)
        if out is None or out[1].warp_index is not None or out[1].connection is not None:
            continue
        node, first = out
        back = nav.step(node, N.OPPOSITE[d])
        if back is None or back[1].dst != (start[0], start[1]) or back[1].warp_index is None:
            continue
        cost = first.cost + back[1].cost
        if best is None or cost < best[0]:
            best = (cost, (first, back[1]))
    return best[1] if best else None


def unreachable_reason(state: MapState, target: Target, base: N.Navigator) -> str:
    """Typed reason a target has no path now: the first cumulative relaxation that creates one
    (``blocked_by_npc`` ... ``forced_movement``), else ``no_path``."""
    for reason, constraints in N.relaxation_levels():
        nav = N.Navigator(state, constraints)
        search = nav.search()
        if plan_target(nav, search, target) is not None:
            return reason
    return "no_path"
