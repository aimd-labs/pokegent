"""Registration for the Pokemon Mansion switch puzzle."""

from __future__ import annotations

from dataclasses import dataclass

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
from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.special import mansion as M
from pokegent.games.gen3.firered.maps.map_motion import predict_step
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, BuilderRegistry, FrameDraft
from pokegent.pipeline import Observation
from pokegent.state.scenes import DetectorRegistry, PriorityClass, SceneResult

FAMILY = "special"
SWITCH_PUZZLE = "special.switch_puzzle"
PRESS_SWITCH = "special.press_switch"
SWITCH_DONE = "special.switch_done"
CLAIMED_SCENES = frozenset()
_MANSION_MAPS = frozenset(item.map_key for item in M.statues()) if M._LOCAL_PUZZLES else frozenset()
_BUTTON = {"N": "up", "S": "down", "W": "left", "E": "right"}
_ALLOWED = frozenset(
    {
        SWITCH_PUZZLE,
        "field.free",
        "scene.unknown",
        "dialog.printing",
        "dialog.advance",
        "dialog.yes_no",
        "dialog.end",
        "dialog.unknown",
    }
)


class MansionDetector:
    name = "special-family:mansion-switch"
    priority = PriorityClass.FIELD
    specificity = 30
    claimed_scenes = CLAIMED_SCENES

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        # Statues are ordinary field interactions and must never replace field.free.
        return None


@dataclass
class MansionBuilder:
    objectives: object | None = None
    navigation: object | None = None
    world: object | None = None

    def build(self, ctx: BuildContext) -> FrameDraft:
        snapshot = ctx.snapshot
        state = snapshot.map
        if state is None or state.player is None:
            return FrameDraft((), fingerprint(SWITCH_PUZZLE, unreadable=True))
        switch_set = M.switch_state(snapshot)
        if switch_set is None:
            # This builder is appended to field.free; an UNKNOWN diagnostic here would become a
            # spurious field choice.  The ordinary field frame already remains safe when flags
            # are unreadable, and the next confirmed snapshot can offer the statues again.
            return FrameDraft((), fingerprint(SWITCH_PUZZLE, unknown=True))
        options: list[Option] = []
        nav = N.Navigator(state)
        search = nav.search()
        map_statues = M.statues_on_map(state)
        for item in map_statues:
            target = T.Target(
                item.id,
                "tile",
                "none",
                f"the {item.floor} Mansion statue at {item.statue}",
                (T.Approach(item.stand, item.facing),),
                at=item.statue,
                info={"x": item.stand[0], "y": item.stand[1]},
            )
            plan = T.plan_target(nav, search, target)
            verify = item.opens[0][0] if not switch_set else item.closes[0][0]
            facts = M.switch_facts(item, switch_set)
            facts.update({"steps": plan.length if plan else None})
            args = {
                "floor": item.floor,
                "x": item.stand[0],
                "y": item.stand[1],
                "facing": item.facing,
                "verify_x": verify[0],
                "verify_y": verify[1],
                "switch_set": switch_set,
            }
            legality = Legality.LEGAL if plan is not None else Legality.UNAVAILABLE
            options.append(
                Option(
                    id=f"switch:{item.floor}:{item.statue[0]}:{item.statue[1]}",
                    label=f"Press the {item.floor} Mansion statue at {item.statue}",
                    description=(
                        "Walk to the stand tile and interact. The game asks whether to press the "
                        "secret switch; answering yes flips the global Mansion state."
                    ),
                    legality=legality,
                    facts=facts,
                    executor_ref=PRESS_SWITCH if plan is not None else None,
                    executor_args=args if plan is not None else {},
                    risk=Risk.NORMAL,
                    postcondition_ref=SWITCH_DONE if plan is not None else None,
                    verb="PRESS_SWITCH",
                    semantic_prompt="A secret switch! Press it?",
                    effect_description=(
                        "Flip the Mansion switch and change the listed gate collision."
                    ),
                    source_task="PokemonMansion_EventScript_SecretSwitch",
                )
            )
        fp = fingerprint(
            SWITCH_PUZZLE,
            map=[state.identity.group, state.identity.number],
            at=[state.player.avatar_x, state.player.avatar_y],
            switch=switch_set,
            options=[[o.id, dict(o.executor_args)] for o in options if o.chooseable],
        )
        context = {
            "map": state.identity.name,
            "switch": "set" if switch_set else "clear",
            "secret_key": list(M.SECRET_KEY_TILE),
            "situation": (
                "The Mansion uses one global switch bit; only the game state changes collision."
            ),
        }
        return FrameDraft(
            tuple(options),
            fp,
            context=context,
            deadline_ms=3000,
        )


def _press_switch(ctx: StepContext):
    state = ctx.snapshot.map
    assert state is not None and state.player is not None
    target = T.Target(
        ctx.option.id,
        "tile",
        "none",
        "the statue stand tile",
        (T.Approach((ctx.args["x"], ctx.args["y"]), ctx.args["facing"]),),
        info={"x": ctx.args["x"], "y": ctx.args["y"]},
    )
    obs = Observation(ctx.snapshot, ctx.scene)
    for _ in range(5):
        current = obs.snapshot.map
        if current is None or current.player is None:
            obs = yield WaitUntil(lambda o: o.snapshot.map is not None, max_frames=30)
            continue
        nav = N.Navigator(current)
        plan = T.plan_target(nav, nav.search(), target)
        if plan is None:
            return
        restart = False
        for move in plan.moves:
            current = obs.snapshot.map
            if current is None or current.player is None:
                restart = True
                break
            player = current.player
            if (player.avatar_x, player.avatar_y) != move.src:
                restart = True
                break
            if player.facing != move.direction:
                obs = yield Tap(_BUTTON[move.direction], hold_frames=4, wait_frames=1)
                continue
            prediction = predict_step(current, move.direction)
            if not prediction.walks or prediction.end_xy != move.dst:
                restart = True
                break
            obs = yield Tap(_BUTTON[move.direction], hold_frames=6, wait_frames=1)
        if restart:
            continue
        current = obs.snapshot.map
        if current is None or current.player is None:
            return
        if current.player.facing != ctx.args["facing"]:
            # The statue occupies the tile ahead. A bare facing tap is required even when the
            # ordinary field option set has no legal press_up (the pre-fork B1F bug).
            obs = yield Tap(_BUTTON[ctx.args["facing"]], hold_frames=4, wait_frames=1)
        obs = yield Tap("a", hold_frames=4, wait_frames=2)
        # A script message may still be printing when the first A returns.  This is especially
        # common when a battle interrupted the approach: the field executor resumes at the
        # statue, but the prompt detector can expose the message one frame before the yes/no
        # choice.  Advance that bounded message phase instead of waiting forever for a prompt
        # that cannot appear until A is pressed again.
        for _ in range(4):
            if obs.scene.scene_id == "dialog.yes_no":
                break
            if obs.scene.scene_id in {"dialog.printing", "dialog.advance", "dialog.end"}:
                obs = yield Tap("a", hold_frames=4, wait_frames=2)
                continue
            obs = yield WaitUntil(
                lambda o: (
                    o.scene.scene_id
                    in {"dialog.yes_no", "dialog.printing", "dialog.advance", "dialog.end"}
                ),
                max_frames=90,
            )
        if obs.scene.scene_id != "dialog.yes_no":
            yield WaitUntil(
                lambda _obs: False,
                max_frames=1,
                failure_reason=_switch_prompt_failure_reason(obs),
            )
            return
        obs = yield Tap("a", hold_frames=4, wait_frames=2)
        yield WaitUntil(
            lambda o: _switch_changed(o, ctx.snapshot, ctx.args["switch_set"]),
            max_frames=180,
        )
        return


def _switch_changed(obs: Observation, before: StateSnapshot, old: bool) -> bool:
    return obs.snapshot.flags is not None and obs.snapshot.flags.is_set(M.SWITCH_FLAG) != old


def _switch_prompt_failure_reason(obs: Observation) -> str:
    """Describe the RAM-observed state when the switch prompt never appeared."""
    state = obs.snapshot.map
    player = state.player if state is not None else None
    identity = state.identity if state is not None else None
    position = f"{player.avatar_x},{player.avatar_y}" if player is not None else "unknown"
    map_name = identity.name if identity is not None else "unknown"
    flag = obs.snapshot.flags.is_set(M.SWITCH_FLAG) if obs.snapshot.flags is not None else "unknown"
    return (
        "switch_prompt_not_observed: "
        f"scene={obs.scene.scene_id}; map={map_name}; position={position}; "
        f"flag_0x{M.SWITCH_FLAG:03X}={flag}"
    )


def _switch_done(ctx: VerifyContext) -> bool:
    before, after = ctx.before, ctx.after
    if before.flags is None or after.flags is None:
        return False
    if before.flags.is_set(M.SWITCH_FLAG) == after.flags.is_set(M.SWITCH_FLAG):
        return False
    old_map, new_map = before.map, after.map
    if old_map is None or new_map is None:
        return False
    tile = (ctx.args["verify_x"], ctx.args["verify_y"])
    return old_map.collision(*tile) != new_map.collision(*tile)


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    objectives=None,
    navigation=None,
    world=None,
    **_: object,
) -> None:
    executors.register(
        ExecutorSpec(
            PRESS_SWITCH,
            ExecutionKind.MACRO,
            _press_switch,
            args={
                "floor": Arg(str),
                "x": Arg(int, min=0, max=255),
                "y": Arg(int, min=0, max=255),
                "facing": Arg(str, choices=("N", "S", "W", "E")),
                "verify_x": Arg(int, min=0, max=255),
                "verify_y": Arg(int, min=0, max=255),
                "switch_set": Arg(bool),
            },
            allowed_scenes=_ALLOWED,
            max_inputs=40,
            max_frames=2400,
            settle_frames=300,
        )
    )
    verifiers.register(VerifierSpec(SWITCH_DONE, _switch_done, max_frames=180, max_wall_s=15.0))


__all__ = ["CLAIMED_SCENES", "FAMILY", "SWITCH_PUZZLE", "register"]
