# Decision idle safety

`emulator.realtime_idle_while_deciding` is opt-in and defaults to false. The existing clock
already had a continuously ticking `idle_frames=True` facility; application startup used
`idle_frames=False`, including with stream realtime pacing. The runner calls `decide()`
synchronously, so a provider wait previously had no command to advance the emulator.

The new mode scopes idle to that synchronous call. A FIFO boundary call releases keys and
installs a read-only guard on the emulator thread. Every normal backend tick still drains audio;
stream readers still capture at actor boundaries. A second FIFO boundary call removes idle before
the runner accepts a choice. The clock cannot tick without an active command after that point.
Executor revalidation and its RAM postconditions are unchanged. Command hold/wait budgets count
only their own frames, and there is still exactly one emulator thread and writer queue.

The guard baseline comes from the atomic sample that produced the decision (`frame + load epoch`
is the live snapshot sequence). An independent composite decoder compares every idle frame with
that sample. It owns its own map/storage caches and text sidecar and never calls the actor,
detectors, builders, route planner or provider from the emulator thread. It decodes the map even
when normal perception disabled map options. The baseline load epoch also must match.

Identity includes all decoded game fields, all UI evidence and task data, map/NPCs, dialog text,
printer position and page, storage and battle data. Exclusions are explicit: snapshot identity,
play time, sampled key history, printer delay counts, and the positive fanfare countdown (its
zero/nonzero boundary is retained). RNG and sprite clocks are not in the decoded model. A decode
error invalidates the choice. Recognized pre-game callbacks may lack a field map; an invalid core
is tolerated only when the original scene already passed the existing `core_invalid` contract.

Invalidation is latched: a cursor moving away and back does not revive a pending choice. External
waits notice invalidation on their normal bounded poll or submission, withdraw the old frame,
and raise `StaleDecision`. Old submissions use the existing `LookupError` → HTTP 409 route.
Other providers are checked after their synchronous return, including exceptional returns. The
runner emits `option.rejected` with `E_STALE_DECISION` and starts a new perception/decision rather
than executing, retrying the old choice, or treating stale input as provider failure. An HTTP
`queued` acknowledgement is provisional; a final idle frame can still invalidate that choice.

## FireRed: what no input establishes

The source argument is conditional on the currently verified input phase. No-input frames do
not establish global game-state immutability. The references below use the same pret revision as
the repository's reader profiles; no game code or extracted data is copied into this tree.

| Phase/case | No-input behavior and stale evidence |
|---|---|
| Ordinary menus/yes-no | `Menu_ProcessInput` and its variants only move/confirm on button events. Cursor animation may continue. Task identity/data, every decoded menu cursor/selection/readiness field and callback are guarded. A script or delayed menu initialization is a transition, not a stable input phase. |
| Dialog waiting for a button | `TextPrinterWait` and `TextPrinterWaitWithDownArrow` wait for A/B only when auto-scroll is disabled. The arrow can animate without accepting input. Printer state/position, text, prompt/page indices and script state are guarded. |
| Printing, auto-scroll, timed text, sound waits | Letters print and auto-scroll can expire without keys. Printer state/position or text/page changes invalidate the choice; script/task changes and fanfare completion also invalidate it. An apparent dialog is never assumed permanently blocked. |
| Player battle action/move/target menu | Normal player handlers gate menu changes/commitment on button events while bounce effects continue. The guard retains controller/phase, every cursor, chooser set, battle script, messages, HP/PP/status and turn data. Tutorial/automatic controllers and phase transitions are not assumed to wait for keys. |
| Battle animations, damage, text, evolution/learning | Automatic handlers can progress without a choice. Battle/UI/core/task changes invalidate the decision even if the original option id and builder fingerprint remain identical. |
| Stationary overworld | Releasing the d-pad does not start an ordinary new walking step. Standard encounter checking is tied to tile-center movement state, rather than a wall timer. A previously started step can finish after key release; NPCs can move, trainers can see the player, and field scripts can run. Live player/map/object/script/UI changes invalidate the choice. |
| Forced movement, map changes, scripts, quest-log playback | Conveyors/ice/current movement, warps, scripted movement and replay-injected game input can advance despite zero hardware keys. Position/movement/map identity, script/task state, callbacks and quest-log evidence are guarded; their changes reject the choice. |
| RNG, play time, visual/audio clocks | Time advances. Exact random outcomes/replay timing may differ, as permitted by this mode; these changes alone do not authorize another scene or selection. All ordinary legality and RAM verification still apply. |

Sources: [menu input](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/src/menu.c),
[text waits and auto-scroll](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/src/text.c),
[player battle handlers](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/src/battle_controller_player.c),
[field input and encounter gating](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/src/field_control_avatar.c).

## Evidence and limits

`tests/test_realtime_decision_idle.py` uses a ROM-free emulator fake and synthetic decoded models.
It checks paced ticks, empty held keys, advancing frame/audio capture, exact FIFO hold/release
counts, one writer thread, cleanup on provider failure, latched transient changes, per-frame read
failure, and stale menu/dialog/NPC/map/script/battle choices with an unchanged builder fingerprint.
It also drives an unchanged decision through the real Executor and its RAM postcondition, and
checks HTTP 409 followed by a new frame/accepted fresh choice. Default-off and strict config
validation are covered.

These tests prove the scheduling and state-guard policy for the represented synthetic cases.
They do not execute FireRed or establish exhaustive correctness of every ROM reader. The source
reasoning above supplies the ordinary input-wait argument; every automatic progression is
handled conservatively as a potential stale state. Native 60 fps throughput/audio continuity
needs host verification: this mode fully decodes every idle frame and may reject frequently in
moving or scripted scenes. It must never be weakened to accept stale state for smoother output.
