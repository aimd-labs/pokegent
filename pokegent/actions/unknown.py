"""Conservative, hybrid-only primitives for an unclassified screen.

No confirm button or game-specific effect is inferred from an unknown scene.
The normal executor owns revalidation, input budgets and the final RAM read.
"""

from __future__ import annotations

from pokegent.actions import Registries
from pokegent.actions._exec.models import Advance, ExecutionKind, Tap
from pokegent.actions._exec.registry import Arg, ExecutorSpec, VerifierSpec
from pokegent.options.models import Legality, Option, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft

WAIT_FRAMES = 30
TAP_FRAMES = 3
BUTTONS = ("B", "UP", "DOWN", "LEFT", "RIGHT")


class UnknownBuilder:
    def build(self, ctx: BuildContext) -> FrameDraft:
        snapshot = ctx.snapshot
        ui = snapshot.ui
        options = [
            Option(
                id="wait:30", label="Wait 30 frames", legality=Legality.LEGAL,
                description="Observe for 30 frames without pressing a button.",
                executor_ref="unknown.wait", postcondition_ref="unknown.observed",
            ),
        ]
        # Missing/invalid UI evidence authorizes observation only. A reported input lock or
        # busy scene also withholds buttons, rather than interpreting unknown as unlocked.
        if ui is not None and ui.valid and not ctx.scene.busy and ui.controls_locked is not True:
            options.extend(
                Option(
                    id=f"tap:{button.lower()}", label=f"Press {button} once",
                    description="One short button press; no game-specific outcome is assumed.",
                    legality=Legality.LEGAL, executor_ref="unknown.tap",
                    executor_args={"button": button}, postcondition_ref="unknown.observed",
                )
                for button in BUTTONS
            )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                ctx.scene.scene_id, core=snapshot.state_hash,
                ui=ui.model_dump(mode="json") if ui is not None else None,
                map=snapshot.map.model_dump(mode="json") if snapshot.map is not None else None,
                battle=(getattr(snapshot, "battle").model_dump(mode="json")
                        if getattr(snapshot, "battle", None) is not None else None),
            ),
            context={
                "screen": "Unclassified screen. Inspect the current screenshot before choosing.",
                "limits": (
                    "Only wait, B, and single direction taps are available. Confirmation inputs "
                    "are withheld because their effects cannot be established. A screen requiring "
                    "confirmation may need supervisor review after the bounded recovery window."
                ),
                "ui_valid": bool(ui is not None and ui.valid),
                "menu": ui.menu_type if ui is not None and ui.valid else None,
                "printer": ui.text_printer if ui is not None and ui.valid else None,
                "controls_locked": ui.controls_locked if ui is not None and ui.valid else None,
            },
        )


def register_unknown_recovery(registries: Registries) -> None:
    """Only the opted-in hybrid assembly calls this; no detector claims are added."""
    registries.builders.register_scenes(("scene.unknown",), UnknownBuilder())
    registries.executors.register(ExecutorSpec(
        "unknown.tap", ExecutionKind.ATOMIC, lambda ctx: Tap(ctx.args["button"]),
        args={"button": Arg(str, choices=BUTTONS)}, max_frames=TAP_FRAMES,
        settle_frames=30, settle_wall_s=2.0,
    ))
    registries.executors.register(ExecutorSpec(
        "unknown.wait", ExecutionKind.DETERMINISTIC, lambda ctx: Advance(WAIT_FRAMES),
        max_frames=WAIT_FRAMES, settle_frames=30, settle_wall_s=2.0,
    ))

    def observed(ctx):
        before, after = ctx.before.emulator_frame, ctx.after.emulator_frame
        frames = WAIT_FRAMES if ctx.option.executor_ref == "unknown.wait" else TAP_FRAMES
        # This verifies the primitive and healthy RAM, not a guessed semantic outcome.
        return (ctx.after.usable and before is not None and after is not None
                and after >= before + frames)

    registries.verifiers.register(VerifierSpec(
        "unknown.observed", observed, max_frames=2, max_wall_s=1.0,
    ))
