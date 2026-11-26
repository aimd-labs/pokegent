"""Frozen contracts of the FireRed navigation domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EdgeKind = Literal[
    "walk",
    "bike",
    "connection",
    "warp",
    "elevator",
    "forced",
    "ledge",
    "surf",
    "dismount",
    "waterfall",
    "fall",
    "field_move",
    "boulder_push",
    "gate",
    "toggle",
    "fly",
    "ferry",
]


@dataclass(frozen=True, order=True)
class NavigationNode:
    map_group: int
    map_number: int
    x: int
    y: int
    elevation: int = 0

    @property
    def map_key(self) -> tuple[int, int]:
        return self.map_group, self.map_number


#: Requirements no capability can satisfy: the route continues only after a player decision
#: the agent must make (the Mt Moon fossil choice).  Kept beside the requirement contract so
#: the planner and the adapter agree on what a decision-blocked edge is.
CHOICE_REQUIREMENTS = frozenset({"fossil"})


@dataclass(frozen=True)
class NavigationRequirement:
    """One edge prerequisite, evaluated from the current RAM snapshot."""

    key: str
    label: str
    met: bool
    source: Literal["party", "badge", "bag", "flag", "var", "state", "rom"]


@dataclass(frozen=True)
class NavigationEdge:
    stable_key: str
    source: NavigationNode
    destination: NavigationNode
    kind: EdgeKind
    cost: int
    requirements: tuple[NavigationRequirement, ...] = ()
    direction: str | None = None
    executor_target: tuple[tuple[str, object], ...] = ()
    forced_path: tuple[tuple[int, int], ...] = ()
    risks: tuple[str, ...] = ()
    prompt: bool = False

    @property
    def available(self) -> bool:
        return all(requirement.met for requirement in self.requirements)

    @property
    def unmet(self) -> tuple[str, ...]:
        return tuple(requirement.label for requirement in self.requirements if not requirement.met)

    def target_dict(self) -> dict[str, object]:
        return dict(self.executor_target)


@dataclass(frozen=True)
class NavigationTarget:
    map_group: int
    map_number: int
    x: int | None = None
    y: int | None = None
    warp_index: int | None = None
    local_id: int | None = None

    @property
    def map_key(self) -> tuple[int, int]:
        return self.map_group, self.map_number


@dataclass(frozen=True)
class NavigationPlan:
    start: NavigationNode
    target: NavigationTarget
    cost: int | None
    route: tuple[NavigationEdge, ...] = ()
    unmet: tuple[str, ...] = ()
    explored: int = 0
    blocked_route_fact: str | None = None
    #: Stable key of the edge that only a decision can unblock (for example the Mt Moon fossil
    #: choice). It is a route step the agent must make, not a missing capability, so the
    #: adapter advertises the option that performs it. ``None`` when a capability (Cut, Surf, a
    #: flag) also blocks the route, and when nothing is blocked.
    blocked_choice_step: str | None = None

    @property
    def reachable(self) -> bool:
        return self.cost is not None and not self.unmet

    @property
    def next_step(self) -> NavigationEdge | None:
        return self.route[0] if self.route else None


@dataclass(frozen=True)
class CapabilityState:
    """Live facts used to evaluate edge requirements."""

    capabilities: frozenset[str] = frozenset()
    items: frozenset[int] = frozenset()
    flags: frozenset[int] = frozenset()
    flags_known: bool = True
    vars: tuple[tuple[int, int], ...] = ()
    vars_known: bool = True
    quest_log_state: int | None = None
    visited_maps: frozenset[tuple[int, int]] = frozenset()
    biking: bool = False
    surfing: bool = False
    strength_active: bool = False
    live_objects: tuple[object, ...] = field(default=(), compare=False)

    def has(self, key: str) -> bool:
        if "&" in key:
            return all(self.has(part) for part in key.split("&"))
        if key.startswith("item:"):
            return int(key.split(":", 1)[1], 0) in self.items
        if key.startswith("flag:"):
            return int(key.split(":", 1)[1], 0) in self.flags
        if key.startswith("var:"):
            if not self.vars_known:
                return False
            parts = key.split(":")
            actual = dict(self.vars).get(int(parts[1], 0), 0)
            if len(parts) == 3:
                return actual == int(parts[2], 0)
            if len(parts) == 4:
                expected = int(parts[3], 0)
                return {
                    "lt": actual < expected,
                    "eq": actual == expected,
                    "gt": actual > expected,
                    "le": actual <= expected,
                    "ge": actual >= expected,
                    "ne": actual != expected,
                }.get(parts[2], False)
            return False
        return key in self.capabilities
