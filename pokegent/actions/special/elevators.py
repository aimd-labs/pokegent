"""Elevator panel detection, labelled floor options, and bounded cursor macros (WP-B2)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pokegent.actions._exec.contracts import ActionContract, ExpectedState
from pokegent.actions._exec.models import ExecutionKind, Tap, WaitUntil
from pokegent.actions._exec.registry import (
    Arg,
    ExecutorRegistry,
    ExecutorSpec,
    StepContext,
    VerifierRegistry,
    VerifierSpec,
    VerifyContext,
)
from pokegent.actions.menu.dialog import evidence as D
from pokegent.actions.menu.dialog.macros import list_position, menu_cursor
from pokegent.games.gen3.firered.maps.world import load_world_adapter
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.validate import ResolvedObjective
from pokegent.local_data import read_data
from pokegent.navigation.objectives import resolve_collect_navigation
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, BuilderRegistry, FrameDraft
from pokegent.state.scenes import DetectorRegistry, PriorityClass, SceneResult

FAMILY = "special"
ELEVATOR_SCENES = frozenset({"dialog.elevator", "menu.elevator"})
CLAIMED_SCENES = ELEVATOR_SCENES
MACRO_SCENES = ELEVATOR_SCENES | frozenset({"field.scripted_movement"})
SELECT_FLOOR = "special.select_floor"
CANCEL = "special.cancel_elevator"
ARRIVED = "special.elevator_arrived"
CLOSED = "special.elevator_closed"
LIFT_KEY_FLAG = 0x2A5
def _table() -> dict[str, dict[str, Any]]:
    return read_data("elevators.yaml")["elevators"]


_LOCAL_TABLE = read_data("elevators.yaml", optional=True)
TABLE = _LOCAL_TABLE["elevators"] if _LOCAL_TABLE is not None else {}
BY_MAP = {tuple(row["map"]): (name, row) for name, row in TABLE.items()}


def _map_key(snapshot: StateSnapshot) -> tuple[int, int] | None:
    if snapshot.player is not None:
        return snapshot.player.map_group, snapshot.player.map_num
    if snapshot.map is not None:
        return snapshot.map.identity.group, snapshot.map.identity.number
    return None


def _elevator(snapshot: StateSnapshot) -> tuple[str, dict[str, Any]] | None:
    key = _map_key(snapshot)
    return BY_MAP.get(key) if key is not None else None


def _has_lift_key(snapshot: StateSnapshot) -> bool:
    return snapshot.flags is not None and snapshot.flags.is_set(LIFT_KEY_FLAG)


class ElevatorDetector:
    def __init__(self, scene_id: str, *, kind: str) -> None:
        self.name = f"special-elevator:{scene_id}"
        self.scene_id = scene_id
        self.priority = PriorityClass.MODAL
        self.specificity = 30
        self.claimed_scenes = frozenset({scene_id})
        self._kind = kind

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        ui = snapshot.ui
        item = _elevator(snapshot)
        if ui is None or not ui.valid or item is None:
            return None
        _name, data = item
        if data["menu"] != self._kind:
            return None
        if self._kind == "multichoice":
            mc, sm = ui.menu.multichoice, ui.menu.menu
            if mc is None or sm is None or not D.multichoice_open(ui):
                return None
            expected_id = data.get("menu_id")
            if expected_id is not None and mc.multichoice_id != expected_id:
                return None
            expected = len(data["floors"]) - 1
            if sm.max_cursor_pos != expected:
                return None
            return SceneResult(
                scene_id=self.scene_id,
                confidence=0.95,
                evidence={"elevator": _name, "entries": expected + 1},
            )
        lm = ui.menu.list_menu
        if not D.list_open(ui) or lm is None:
            return None
        if lm.total_items != data["total"] or lm.max_showed != data["shown"]:
            return None
        return SceneResult(
            scene_id=self.scene_id,
            confidence=0.95,
            evidence={"elevator": _name, "entries": lm.total_items, "shown": lm.max_showed},
        )


def build_detectors() -> tuple[ElevatorDetector, ...]:
    return (
        ElevatorDetector("dialog.elevator", kind="multichoice"),
        ElevatorDetector("menu.elevator", kind="list"),
    )


class ElevatorBuilder:
    def __init__(
        self,
        table: Mapping[str, Mapping[str, Any]] = TABLE,
        *,
        objectives=None,
        navigation=None,
        world=None,
    ) -> None:
        self.table = table
        self.objectives = objectives
        self.navigation = navigation
        self.world = world

    def build(self, ctx: BuildContext) -> FrameDraft:
        item = _elevator(ctx.snapshot)
        if item is None:
            return FrameDraft(
                (),
                fingerprint(ctx.scene.scene_id, missing="elevator map"),
                {"summary": "Elevator map is unreadable."},
            )
        name, data = item
        locked = data.get("requires") == "lift_key" and not _has_lift_key(ctx.snapshot)
        options: list[Option] = []
        for entry, floor in enumerate(data["floors"]):
            destination = floor["map"]
            facts: dict[str, object] = {
                "floor": floor["label"],
                "destination": floor["place"],
                "destination_map": destination,
                "current_floor": name,
                "needs_key": not locked,
                "required_flag": LIFT_KEY_FLAG if data.get("requires") == "lift_key" else None,
                "target": {"kind": "elevator", "dest": destination},
            }
            # Silph's script keeps the elevator map loaded and commits the selected
            # destination through VAR_ELEVATOR_FLOOR before exposing the dynamic warp.
            # Keep this RAM postcondition available even when the navigation graph is
            # not wired into a small/specialized rig.
            if name == "silph" and floor["code"] != "exit":
                facts.update({"floor_var": 0x403A, "floor_value": 14 - entry})
            if self.navigation is not None:
                graph = getattr(self.navigation, "graph", None)
                key = _map_key(ctx.snapshot)
                if graph is not None and key is not None:
                    decoded = next(
                        (
                            edge.target_dict()
                            for edge in graph.elevator_edges(key)
                            if edge.target_dict().get("floor_map") == tuple(destination)
                        ),
                        {},
                    )
                    if decoded:
                        facts.update(
                            {
                                "floor_var": decoded.get("floor_var"),
                                "floor_value": decoded.get("floor_value"),
                                "floor_writes_var": decoded.get("floor_var") is not None,
                            }
                        )
            legality = Legality.UNAVAILABLE if locked else Legality.LEGAL
            reason = "; Lift Key is not present" if locked else ""
            options.append(
                Option(
                    id=f"floor:{floor['code']}",
                    label=floor["label"],
                    description=f"Go to {floor['label']}: {floor['place']}{reason}.",
                    legality=legality,
                    executor_ref=SELECT_FLOOR if not locked else None,
                    executor_args={
                        "entry": entry,
                        "dest_group": destination[0],
                        "dest_number": destination[1],
                        "is_exit": floor["code"] == "exit",
                    },
                    postcondition_ref=ARRIVED if not locked else None,
                    risk=Risk.NORMAL,
                    verb="SELECT_FLOOR",
                    facts=facts,
                    expected_postcondition=(
                        "The elevator ride sets the selected floor and exposes its dynamic warp."
                    ),
                    action_contract=ActionContract(
                        cursor_key=(
                            f"choice:{entry}"
                            if ctx.scene.scene_id == "dialog.elevator"
                            else f"elevator:{name}:{floor['code']}"
                        ),
                        expected=(
                            ExpectedState(
                                kind="map_changed",
                                map_group=destination[0],
                                map_number=destination[1],
                            ),
                        ),
                    ),
                )
            )
        options.append(
            Option(
                id="cancel",
                label="Cancel",
                description="Close the elevator menu without changing floors.",
                legality=Legality.LEGAL,
                executor_ref=CANCEL,
                postcondition_ref=CLOSED,
                risk=Risk.LOW,
                verb="CANCEL",
                facts={"floor": "none", "current_floor": name, "escape": True},
                expected_postcondition="The elevator menu closes.",
                action_contract=ActionContract(
                    expected=(ExpectedState(kind="menu_closed", surface=ctx.scene.scene_id),),
                ),
            )
        )
        context = {
            "summary": f"The {name} elevator floor menu is open.",
            "elevator": name,
            "labels_verified": True,
        }
        if self.navigation is not None and self.objectives is not None and ctx.goal_id is not None:
            got = self.objectives(ctx.goal_id)
            resolved = got if isinstance(got, ResolvedObjective) else None
            resolved, _sources = resolve_collect_navigation(ctx.snapshot, resolved, self.world)
            plan = self.navigation.plan(ctx.snapshot, resolved)
            options, plan = self.navigation.annotate(options, ctx.snapshot, resolved, plan=plan)
            if plan is not None and plan.unmet:
                context["blocked_route"] = self.navigation.blocked_route(plan)
        return FrameDraft(
            tuple(options),
            fingerprint(
                ctx.scene.scene_id,
                map=_map_key(ctx.snapshot),
                cursor=ctx.snapshot.ui.menu.menu.cursor_pos
                if ctx.snapshot.ui and ctx.snapshot.ui.menu.menu
                else None,
            ),
            context,
            deadline_ms=10000,
        )


def _tap(button: str) -> Tap:
    return Tap(button, hold_frames=4, wait_frames=2)


def _select_floor(ctx: StepContext):
    entry = int(ctx.args["entry"])
    if ctx.scene.scene_id == "dialog.elevator":
        start, read = menu_cursor(ctx.snapshot), menu_cursor
    else:
        start, read = list_position(ctx.snapshot), list_position
    if start is None:
        raise RuntimeError("elevator cursor is unreadable; A not pressed")
    current = start
    for _ in range(128):
        if current == entry:
            yield _tap("a")
            expected_var = ctx.option.facts.get("floor_var")
            expected_value = ctx.option.facts.get("floor_value")

            def ride_finished(obs) -> bool:
                snap = obs.snapshot
                if obs.scene.scene_id != "field.free" or obs.scene.busy:
                    return False
                if not isinstance(expected_var, int) or not isinstance(expected_value, int):
                    return True
                return snap.vars is not None and snap.vars.get(expected_var) == expected_value

            yield WaitUntil(ride_finished, max_frames=1200, max_wall_s=60.0, check_every=4)
            return
        direction = "down" if entry > current else "up"
        observed = yield _tap(direction)
        current = read(observed.snapshot)
        if current is None or not 0 <= current <= 127:
            raise RuntimeError("elevator cursor read-back mismatch; A not pressed")
        start = current
    raise RuntimeError("elevator cursor budget exhausted; A not pressed")


def _cancel(_ctx: StepContext) -> Tap:
    return _tap("b")


def _arrived(ctx: VerifyContext) -> bool:
    after = ctx.after.player
    before = ctx.before.player
    if after is None or before is None:
        return False
    if (after.map_group, after.map_num) != (ctx.args["dest_group"], ctx.args["dest_number"]):
        # FireRed's elevator script sets the dynamic warp and updates VAR_ELEVATOR_FLOOR,
        # then closes the menu while still on the shared elevator map.  The field warp is a
        # separate action, so map-changed is not the ride postcondition in that boundary.
        facts = ctx.option.facts
        expected_var = facts.get("floor_var")
        expected = facts.get("floor_value")
        if (
            ctx.args.get("is_exit") is True
            and ctx.scene.scene_id == "field.free"
            and (before.map_group, before.map_num) == (after.map_group, after.map_num)
        ):
            # EXIT does not write VAR_ELEVATOR_FLOOR; it only closes the panel.  The
            # existing dynamic warp remains valid for the current floor.
            return True
        return (
            isinstance(expected_var, int)
            and isinstance(expected, int)
            and ctx.after.vars is not None
            and ctx.after.vars.get(expected_var) == expected
            and ctx.scene.scene_id == "field.free"
            and (before.map_group, before.map_num) == (after.map_group, after.map_num)
        )
    return True


def _closed(ctx: VerifyContext) -> bool:
    return ctx.scene.scene_id not in ELEVATOR_SCENES


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    objectives=None,
    navigation=None,
) -> None:
    for detector in build_detectors():
        detectors.register(detector)
    world = load_world_adapter() if objectives is not None and navigation is not None else None
    builders.register_scenes(
        ELEVATOR_SCENES,
        ElevatorBuilder(objectives=objectives, navigation=navigation, world=world),
    )
    executors.register(
        ExecutorSpec(
            SELECT_FLOOR,
            ExecutionKind.MACRO,
            _select_floor,
            args={
                "entry": Arg(int, min=0, max=11),
                "dest_group": Arg(int, min=0, max=126),
                "dest_number": Arg(int, min=0, max=127),
                "is_exit": Arg(bool),
            },
            allowed_scenes=MACRO_SCENES,
            max_inputs=64,
            max_frames=1200,
            settle_frames=900,
            settle_wall_s=60.0,
            macro_manages_busy=True,
        )
    )
    executors.register(
        ExecutorSpec(CANCEL, ExecutionKind.ATOMIC, _cancel, allowed_scenes=ELEVATOR_SCENES)
    )
    verifiers.register(VerifierSpec(ARRIVED, _arrived, max_frames=180, max_wall_s=20.0))
    verifiers.register(VerifierSpec(CLOSED, _closed, max_frames=120, max_wall_s=10.0))


__all__ = [
    "BY_MAP",
    "CLAIMED_SCENES",
    "ELEVATOR_SCENES",
    "TABLE",
    "ElevatorBuilder",
    "build_detectors",
    "register",
]
