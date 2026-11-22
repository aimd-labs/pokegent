# Contracts v1.0 (M2.0 freeze) + additive v1.1 (M2.5, section 10), v1.2 (section 11), v1.3 (M3.2, section 12), v1.4 (M4 fix, section 13), v1.8 (endgame gaps, section 17)

Implement scene families against this page. Source of truth for behavior: spec chapters 5, 6, 8.1,
11, 13.4; this page fixes the shapes and the choices the spec left open. Code: `options/models.py`,
`options/registry.py`, `state/{catalog,scenes}.py`, `pipeline.py`, `actions/_exec/*`, `events.py`,
`loop.py`, `errors.py`. Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.

## 1. Flow and ownership

```text
SnapshotSource -> DetectorRegistry -> Perceiver -> BuilderRegistry(family) -> DecisionFrame
   (reader)       (SceneResult)       (pipeline)    (FrameDraft + identity)       |
                                                          forced | Decider -> option_id
                                                                        |
EmulatorActor <- Executor (re-validate on a FRESH snapshot, run, verify) <- Runner -> EventLog
```

| Piece | Owns | Must not |
|---|---|---|
| Detector | one claim per snapshot | press, wait, read the emulator |
| Builder | all options + legality + fingerprint + English context | press, call a model, assign ids, hide UNKNOWN as LEGAL |
| Decider | pick one id from `chooseable()` | see `executor_ref/args`, pointers; return an id outside the frame |
| Executor | the only input path; re-validation; budgets; verification | run a stale option, run unregistered code, write RAM |
| Runner | loop, routing, counters, events, escalation | decide strategy, press directly, call agent on the button path |

Decision order (CONTEXT.md): agent plan/overlay > SYSTEM1 with facts > default. In code: agent acts only
through the `EscalationSink` (structured response `CONTINUE|PAUSE|STOP`) and, later, by changing the
goal that builders read (`BuildContext.goal_id`); SYSTEM1 is the `Decider`; the default is the
deterministic ranking a builder puts first and the forced/deterministic options. Nothing above the
Executor can name a button.

## 2. Types (all Pydantic v2, frozen, `extra=forbid` unless noted)

In `battle.action`, `move:N` options carry `facts.slot = N`, where `N` is the move index 0-3;
it is not the party slot.

**Legality** `LEGAL | CONDITIONAL | UNAVAILABLE | UNKNOWN | UNSUPPORTED`. Chooseable = the first two.

**Option** `id` (`^[a-z][a-z0-9_]*(:[A-Za-z0-9_.-]+)*$`, stable and semantic: `move:1`, `warp:<map>:<idx>`),
`label`, `description` (English), `legality`, `facts` (JSON-like, frozen), `executor_ref`
(`namespace.name`, required if chooseable), `executor_args`, `risk` (`low|normal|high`),
`postcondition_ref`, `verb` (optional spec-6 vocabulary token), and per 6.C `semantic_prompt`,
`effect_description`, `irreversible`, `source_task`, `expected_postcondition`.
`irreversible=True` requires prompt, effect text and `risk=high`.

**DecisionFrame** `schema_version "1.0"`, `decision_id`, `run_id`, `snapshot_seq`, `scene_id`,
`scene_confidence`, `state_fingerprint` (`sha256:<64 hex>`), `goal_id`, `decision_type "choice"`,
`deadline_ms`, `context` (frozen, English, model-visible), `options` (the chooseable set ONLY),
`diagnostics` (non-chooseable options, never offered). Ids unique across both lists. Deeply immutable.

**ChoiceResult** `option_id`, `probabilities`, `confidence`, `model_id`, `provider_id`, `latency_ms`,
`truncated`, `raw_response_ref`, optional `decision_id` (echo; a mismatch is `E_STALE_DECISION`).
`Decider.decide(frame) -> ChoiceResult` is synchronous; async providers adapt to it.

**System One payload** `to_system_one_payload(frame)` = `{state, instructions, criteria}` only;
`criteria = {option_id: concise English text}` for chooseable options. Fails closed
(`PayloadLeakError`) if refs, args, pointers, fingerprints or ids appear in any text.

**SceneResult** `scene_id`, `confidence` 0..1, `evidence` (frozen dict), `busy`, `detector`.
Reserved ids outside chapter 6: `system.invalid`, `scene.unknown`.

**ActionResult** `status` (`applied|rejected|interrupted|timed_out|uncertain`), `before_hash`,
`after_hash`, `input_trace` (`InputStep`s: `tap` or `advance`), `game_event_ids` (reserved, empty until
snapshots carry game events), `elapsed_frames`, `verified_outcome` (`bool|None`), `error_code`,
`reason`, plus `decision_id`, `option_id`, `executor_ref`, `kind`.

**EventEnvelope** `schema_rev` (int, 1), `id` (`<run_id>:<seq>`), `run_id`, `seq` (from 1, no gaps),
`observed_at` (ISO UTC), `emulator_frame`, `type`, `scene_id`, `request_id`, `provider`, `outcome`,
`source`, `public_visibility` (bool, default False), `details`.

## 3. Invariants (each has a test)

Opt-in decision idle is an observation exception to command-only frame advancement:
`emulator.realtime_idle_while_deciding` (default false) scopes paced no-input frames to a
decider wait. The single actor latches any decoded observable-state change on every idle frame;
`E_STALE_DECISION` withdraws the old decision and requests a fresh one (external submissions use
HTTP 409). Idle stops at a boundary before executor revalidation; no idle tick can intervene
between that validation and input. Existing input FIFO/frame budgets and RAM verification remain
unchanged. See [decision idle safety](realtime-idle.md) for identity, FireRed cases and limits.

1. `options` holds only LEGAL/CONDITIONAL; ids unique per frame; a frame never changes after creation.
2. The model sees `state`, `instructions`, `criteria` and nothing else; it answers with an option id.
3. Executor input requires, on a fresh snapshot: usable, `snapshot_seq` not older, same `scene_id`,
   not busy, same `state_fingerprint`, option still chooseable with identical `executor_ref/args`.
   Otherwise `rejected`, `input_trace == ()`, `elapsed_frames == 0`.
4. `executor_ref`/`postcondition_ref` resolve only in local registries; unknown means `rejected`.
5. Every wait is bounded in frames AND wall seconds (independent clocks); every action in inputs and
   frames (`ExecutorSpec.max_inputs/max_frames`); exhausting a bound is `timed_out`.
6. A macro is checked at every step boundary against its `allowed_scenes` (default: the option's scene);
   busy phases are waited out (bounded); anything else stops it (`interrupted`, remaining steps unsent).
7. A tap is logged before the actor call, so an uncertain press is never reported as "no input".
8. Unknown/unsupported/escalation-only scene, no builder, no chooseable option, invalid snapshot:
   no input; diagnostics event; escalation (or SAFE_PAUSE for invalid/unsupported ROM).
   Additive hybrid opt-in: `hybrid.escalate_unknown_scene` (default true) may offer an unknown
   scene to the agent after bounded settles. It offers only generic wait/B/direction primitives,
   with no confirmation inputs or assumed game-specific effects. Every choice passes the same
   fresh scene, fingerprint, option, input-budget and RAM/frame verification gates. This uses
   rule 10's single bounded agent window (including wait choices), even when no-progress
   escalation is disabled. A screen requiring A may still exhaust the window and SAFE_PAUSE.
   Other deciders and invalid/unsupported-state handling are unchanged.
9. Exactly one chooseable option: executed without calling the decider (`decision.forced`).
10. The same (scene, option, fingerprint) attempted `no_progress_limit` times inside
    `no_progress_window` iterations: `progress.stuck` + escalation.
    Hybrid may route the first no-progress verdict to its agent for one bounded recovery window
    (`hybrid.escalate_on_no_progress`, default true). All action-bearing decisions in this window
    bypass System One with `escalation_reason=no_progress`. Another exhausted window or earlier
    guard verdict SAFE_PAUSEs; only executor-verified durable progress (`result.progressed is True`)
    ends and rearms recovery. Position-only movement and monitor signature changes do not rearm it.
    Non-hybrid modes retain their existing escalation behavior.
11. No randomness, no wall clock in decisions. Event ids and decision ids are sequential. The only
    non-replayable field is `details.measured_ms` of `decision.systemone.completed`.
12. Secrets are redacted before an event is written; one flush per event; rotation never overwrites.

## 4. Scene detection (spec 6.0)

Priority classes (lower wins): SYSTEM 1, MODAL 2, BATTLE 3, MENU 4, FIELD_DIALOG 5, FIELD 6, BUSY 7.
Order: class, then specificity (higher wins), then confidence, then registration order. Ambiguity
(equal class and specificity, confidence within 0.1) is `scene.unknown` unless exactly one candidate is
an expected successor of the previous scene. Also `unknown`: top confidence < 0.6; top < 0.8 against a
more confident lower-class claim; a raising detector; an id not in the catalog. A confident BUSY claim
next to a ready scene sets `busy`. YES/NO scenes (from the catalog) are confirmed over 2 of up to 3
snapshots (`ConfirmPolicy`, frames advanced through the executor only).

`references/scenes.yaml` records the public scene interface vocabulary (224 rows). `family` = id namespace = builder-registry key. `phases` come from the
heading (`P0/P1` for most sections, coarse: 177 rows are P0-capable) or the row's own phase column.
P2-only rows and `UNSUPPORTED*` rows are unsupported; `system.unsupported_rom` pauses;
`dialog.unknown`, `field.unknown_obstacle`, `special.unknown_script` escalate.

## 5. Implementing a scene family (checklist)

Detector(s) with priority/specificity and ids from the catalog; one `OptionBuilder` registered for the
family returning a `FrameDraft` (options of every legality, `fingerprint(scene_id, **relevant)`,
English `context`); executor specs (`atomic` = one tap, `macro`/`intent` = generator yielding
`Tap|Advance|WaitUntil` and receiving an `Observation` after each step, `deterministic` = waits) with
`allowed_scenes`, budgets and `Arg` declarations; verifier specs with bounded waits; fixtures proving
detect + legal-set + execute + verify. Report scene ids through `SceneCoverageReport`.

## 6. Error codes (`errors.ErrorCode`, never renamed)

Perception: `E_SNAPSHOT_INVALID E_STALE_SNAPSHOT E_SCENE_CHANGED E_FINGERPRINT_CHANGED E_SCENE_BUSY
E_SCENE_UNKNOWN E_SCENE_UNSUPPORTED`. Option/choice: `E_UNKNOWN_OPTION E_NOT_CHOOSEABLE E_OPTION_GONE
E_OPTION_CHANGED E_CHOICE_NOT_IN_FRAME E_CHOICE_MALFORMED E_STALE_DECISION`. Registries:
`E_UNKNOWN_EXECUTOR_REF E_UNKNOWN_POSTCONDITION E_BAD_ARGS`. Execution: `E_INPUT_REJECTED
E_INTERRUPTED_SCENE E_WAIT_TIMEOUT_FRAMES E_WAIT_TIMEOUT_WALL E_BUDGET_EXCEEDED
E_POSTCONDITION_NOT_MET E_EMULATOR`. Decider: `E_DECIDER_TIMEOUT E_DECIDER_FAILED E_PAYLOAD_LEAK`.
Runner: `E_NO_BUILDER E_NO_LEGAL_OPTION E_NO_PROGRESS E_BUSY_STUCK`.
Status meaning: `rejected` = nothing pressed; `interrupted` = macro stopped at a boundary;
`timed_out` = a bound ran out while inputs may have run; `uncertain` = inputs ran, outcome not
established (postcondition unmet, actor failure).

## 7. Events

Minimum list of spec 13.4 plus `decision.forced` and `escalation.requested`. Runner emits:
`runtime.{started,paused,recovering,recovered,resumed,blocked,degraded,stopped}`, `scene.{entered,
unknown,unsupported}`, `options.built`, `decision.{forced,systemone.*}`, `action.{started,input,
verified,interrupted,failed}`, `option.rejected`, `progress.stuck`, `escalation.requested`.

In hybrid mode, a System One result that is escalated is recorded immediately, before waiting for
the agent, as `decision.systemone.completed` with `details.accepted == false` (including `choice`,
`confidence`, and `measured_ms`), followed by `decision.agent.requested` with the served `frame_id`
and `escalation_reason`. Provider failures and timeouts use the same hand-off sequence with a null
choice/confidence; the later accepted agent choice is `decision.agent.completed`. The agent-requested
event is emitted immediately after the frame is published, so a client may observe the frame first.
Bounded hybrid escalations also carry an agent-only top-level `handoff` record (`handoff/1`) on
the served frame; it is not metadata and never enters the System One payload. Its
`objective.revision` is captured with the frame at build time (null when unknown), and
`deadline.remaining_s` is derived from the existing private monotonic deadline, never a wall
clock.
Unknown types raise at `emit` (a programming error); readers accept any well-formed type.

Hybrid adaptive routing may bypass System One after consecutive executed choices of the same
option on an unchanged scene/fingerprint without verified state change. The agent request then
uses `escalation_reason=repeated_option`; no System One completion is fabricated for the bypassed
call. Agent choices still pass normal frame and executor validation.

## 8. Frozen vs may change

**Frozen (v1.x, additive only):** field names and meanings of Option, DecisionFrame, ChoiceResult,
SceneResult, ActionResult, EventEnvelope; the Legality and ActionStatus vocabularies; option-id
grammar; error-code names; the System One payload shape; rules 1-12 above; builder, detector, executor
and verifier registration shapes (`OptionBuilder.build`, `SceneDetector.detect`, `ExecutorSpec`,
`VerifierSpec`, `EscalationSink`, `Decider`).

**May still change (announce in the PR):** numeric defaults (confidence thresholds, budgets,
retry counts, `no_progress_*`); the instructions text; new optional fields and event/error types;
`BuildContext.world` (WP4); `StateSnapshot` gains `ui/map/battle` blocks (WP2/WP4, minor bump);
`game_event_ids` filling; the `intent` kind's navigator; confirmation scope; per-scene phase refinement
(M2 scope is finer than the heading phases); a `Decider` async variant.

## 9. Choices made beyond the spec

`diagnostics` split from `options`; builder-defined `state_fingerprint` (digest of decision-relevant
parts, compared by equality on the rebuilt frame) instead of the snapshot `state_hash`; builders return
`FrameDraft`, the pipeline assigns identity; `Option.verb` for the wrong-options check; sync `Decider`
with an optional `decision_id` echo; irreversible options require risk `high`; macros as generators
with `allowed_scenes`; `timed_out` (gate waits) vs `uncertain` (postcondition) split; fresh-snapshot
check includes `snapshot_seq` monotonicity; unknown scenes get bounded settle retries before
escalation; invalid snapshot emits SAFE_PAUSE first, then bounded recovery, then `runtime.blocked`;
deterministic event ids; phases and families taken mechanically from the headings.

## 10. Additive v1.1 (M2.5 integration)

Announced here, nothing above was renamed or removed; every v1.0 test still passes unchanged. Each item
is optional for a family or a caller.

1. **Transient invalid reads are retried centrally.** `pipeline.Perceiver` reads through one path
   (`_read`): an invalid snapshot whose read errors are ALL in `TRANSIENT_INVALID_CODES`
   (`party/mon_bad_checksum`: the game decrypts a mon in place while a party or summary screen draws,
   WP1c) is read again one settled frame later, at most `transient_retries` (default 4) times in a row;
   the 5th consecutive invalid frame is returned as invalid (SAFE_PAUSE path of rule 8). Any other invalid
   reason is returned at once. The retry is `settle` (observation), never input; `observe`, `perceive` and
   the confirmation re-reads all use it, so the executor's boundary reads and busy waits benefit. New:
   `Perceiver(transient_retries=, transient_codes=)`, `RunnerConfig.transient_invalid_frames` (default 4),
   counter `ProgressCounters.transient_invalid_settled`. Replaces the battle family's `SettlingPerceiver`.
2. **`ExecutorSpec.settle_wall_s`** (default `None` = `Executor.busy_wait_wall_s`): wall-clock bound of the
   post-step busy wait, next to `settle_frames`. Rule 5 stands (frames AND wall, independent clocks).
3. **Composite snapshot source** (`pokegent/games/gen3/firered/reader/snapshot_sources.py`): ONE atomic snapshot per
   perception step carries the core, the `ui` block, the `map` block (outside battles) and the `battle`
   block (a `StateSnapshot` subclass `BattleSnapshot`, read with `getattr(snapshot, "battle", None)`; not
   part of `state_hash`), plus the dialog text in a sidecar keyed by `(snapshot_seq, state_hash)`.
   `StateSnapshot` itself is unchanged. Owners: core = WP1 reader, `ui` = M2.1, `map` = M2.2, `battle` =
   battle family, dialog text = dialog family.
4. **`register_all` and unique scene ownership** (`pokegent/actions`): `register_all(Registries,
   FamilyConfig)` registers dialog + battle and, if the package exists, optional families
   (`register_optional("field")`). A scene id claimed by detectors or builders of two families raises
   `OwnershipError` unless listed in `FamilyConfig.shared_scenes`. Families declare claims through a detector
   attribute `claimed_scenes` and module constants `CLAIMED_SCENES`, `BUSY_SCENES`, `UNSUPPORTED_SCENES`,
   `FOLDED_SCENES`, `KNOWN_GAPS`, `CONFIRM_SCENE_IDS`. Read-only accessors `BuilderRegistry.family_builders()` /
   `scene_builders()`. `FamilyBundle.ownership(catalog)` is the per-catalog-scene table (`pokegent
   scenes`); P0 scenes nobody detects are listed as gaps. `dialog` owns `system.whiteout` and
   `system.quest_log_playback`.
5. **Confirmation scope.** `FamilyBundle.confirm_policy(catalog)` = the catalog's YES+NO rows UNION each
   family's `confirm_scene_ids` (`dialog.heal_offer`).
6. **Postconditions must hold at the first stable state after the commit** (battle friction 2). A
   verification wait cannot press keys, so a predicate that needs the NEXT decision to be made first
   (a text waiting for A) can never hold; write it for the state right after the last input.
7. **Diagnostics.** `scene.unknown` / `scene.unsupported` event details gain `ui` (`callback`, `tasks`,
   `menu`, `printer`, `controls_locked`, `in_battle`) so an unknown screen can be classified without a
   replay. No new event type, no new error code.

The operator decision event family is named `decision.agent.*`.
8. **Profile.** `RomProfile.rom_tables` (address, size, source, verified_by) models the `rom_tables`
   section; 22 battle symbols (`gBattleMons` ... `gSideTimers`) moved from `actions/battle/symbols.py`
   into `references/rom_profiles.yaml` with their provenance. `BattleBlock.sides` (`SideInfo` per side).
9. **Offline runner.** `pokegent.app.build_runner`, `run_offline`, CLI `pokegent run`. Output:
   `events.jsonl` (the v1.0 envelope) and `summary.json` (not frozen; fields in `the runtime contracts`).
   Exit codes: 0 budget/requested stop, 1 SAFE_PAUSE or stop, 2 usage, 3 escalation pause,
   4 environment, 124 graceful-stop deadline, 130 forced interrupt.


## 11. v1.2 (additive): option facts reach the model

**Problem (found 2026-10-08 on a real battle frame).** `to_system_one_payload` hands System One only
`criteria {option_id: text}`. The facts that carry a decision (damage range, KO chance, who moves first,
what a potion restores, how far a walk is) lived in `Option.facts` and never reached the model; the
families wrote only some of them into the description by hand.

**Contract.** `criterion_text(option)` appends `Consequences: <rendered facts>` produced by
`options/render.py::render_facts(option.facts)`:

- **Whitelist.** Only fact keys with a registered renderer are shown (move/damage/speed/side effects/
  restrictions, switch, item, ball, field). Unknown keys are ignored, never dumped. The payload leak
  guard still runs on the final text (a renderer that echoes an executor argument fails closed).
- **Facts, not advice.** Wording states consequences ("may knock it out this turn", "acts second",
  "crosses 3 tall-grass tiles"), never "best" or "recommended". Strategy stays with agent and SYSTEM1.
- **Deterministic, bounded, defensive.** Fixed renderer order = priority; same facts give the same
  bytes; cut at a phrase boundary at 320 characters (`DEFAULT_MAX_CHARS`); a fact of an odd shape is
  skipped and counted in `render.STATS["renderer_errors"]`, it never raises.
- **Extension.** A family with its own fact keys calls `register_fact_renderer(name, fn)`; the
  existing renderers and their wording are frozen (additive changes only).
- **What families still write into `description`:** identity and static data (name, type, power,
  accuracy, PP, HP of a Pokemon). Consequences go into `facts`, not into the description text.

Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.


## 12. v1.3 (additive, M3.2): the goal is dynamic and agent steers through the objective file

Nothing above was renamed or removed; every v1.0-v1.2 test passes unchanged. Page of the file contract:
`docs/objective-file.md`. Code: `goals/source.py`, `goals/tracker.py`, `pipeline.py`, `loop.py`, `app.py`.

**Problem.** `Perceiver.goal_id` was fixed at construction, but agent changes the objective while the run goes
on. agent also needs a way to be told at the events it named, without the game ever waiting for it.

1. **`GoalSource` (Protocol, `goals/source.py`).** `current() -> GoalView | None`; `GoalView(goal_id, summary,
   revision)`, the summary English and bounded (400 characters, cut at a word). `Perceiver(goal=<GoalSource>)` asks it
   ONCE per `perceive()`; `FixedGoal` adapts the old `goal_id=` string (still accepted, still settable as
   `perceiver.goal_id`). A source that returns `None` falls back to the fixed id; a source that raises is goal-less for
   that perception. `Perceiver.bind_goal`, `Perceiver.goal_view()`. Only the structured objective
   `forbid` list is hard; prohibition-like phrases in prose (for example, “do not use the PC” or
   “no Fly”) produce one advisory warning and are never inferred into `forbid`.
2. **The objective in the frame.** `BuildContext.objective` (new optional field) carries the summary; the registry
   writes it into `DecisionFrame.context["objective"]` (bounded, overriding a builder's own line, so every family's
   frame shows the same goal text). `frame.goal_id` is the objective id. `to_system_one_payload` therefore shows
   `state.goal` and `state.objective`; revision, probes, `set_by`, the file path and ids never appear (the leak guard
   and a test pin it). Without a goal nothing changes (the field builder keeps its "none set" line).
3. **`ObjectiveSession`** (store + tracker + optional script behind ONE consistent view per iteration). The runner
   calls `refresh()` at the start of an iteration (`os.stat` unless the file changed; never raises; a corrupt, too
   large, schema-invalid or stale file keeps the last valid objective), then `observe(snapshot)` after the perception
   (fresh success probes and bounded stuck detection). The goal a frame was built under is the goal when the executor re-validates it, even if
   agent writes a new file in between; a newer revision is applied by the next perception (`superseded()` makes the
   runner re-perceive instead of acting under the old one). `FamilyConfig.objectives` / `.graph` pass the session's
   objective source and a route graph to a family's `register(objectives=, graph=)` (`register_optional` supplies them by
   parameter name). Probe satisfaction is recomputed from the fresh snapshot for each iteration;
   unreadable probe inputs are unknown and do not satisfy or replace the objective.
4. **Events** (no new event type).
   * `progress.goal_changed` (revision change, also the first objective and the fall back to goal-less),
     `progress.milestone_completed` (partial and `completed: true`, once per fact), `progress.stuck` (the tracker's
     idle detector; the runner's own `progress.stuck` for no-progress/busy streaks now also names `objective_id` and
     `revision`), all with `source="objective"` where the tracker emits them.
   * `runtime.degraded` with `details.component == "objective_store"` for every store warning (`code`, `detail`,
     `revision`, `reasons`, `kept` = the objective that stays active): `unreadable_file`, `file_too_large`,
     `schema_invalid`, `unknown_map`, `stale_revision`, `file_missing`, `expired`, `session_error`,
     `shop_method_unavailable`.
   * `escalation.requested` with `details.kind == "objective_review"` (`EscalationKind.OBJECTIVE_REVIEW`, no
     `error_code`): `objective_id`, `revision`, `trigger: stuck`,
     `review_pending`, `trigger_details`. Requires explicit `next_review_on: ["stuck"]`;
     the default is empty. The same request goes to the `EscalationSink`, whose configured
     review response is continue/pause/stop. It cannot authorize completion, replacement
     or a fallback, and a stale request cannot stop a concurrent successor. Queue capacity64
     and counters capped at2^31-1 bound review bookkeeping. Existing hard safety/recovery
     limits stay authoritative even when the review response is continue.
   * `runtime.paused` with `outcome` `waiting_objective` (pause policy) or `holding` (hold policy, followed by
     `runtime.resumed` when a newer revision arrives).
5. **Review triggers.** Only `next_review_on: ["stuck"]` opts an objective into review; the default is empty.
   Unsupported events and `abort_if` remain rejected. Stuck signals come from the tracker's usable-idle detector
   or the runner's existing no-progress/busy verdicts. Tracker `progress.stuck` telemetry may be emitted without
   review opt-in; it does not itself request review or complete the objective. Progress re-arms one request per
   episode. Existing hard safety/recovery limits remain authoritative regardless of the review response.
6. **Review responses.** The sink's answer is applied as `CONTINUE` / `PAUSE` / `STOP`; no separate reviewer wait
   is added. The application's `RecordingSink` defaults to `CONTINUE` for reviews, configured through
   `AppConfig.on_objective_review`. The library runner's default `PauseOnEscalation` answers `PAUSE`, including
   for reviews. A continue response retains the last valid objective; `RunSummary.review_pending` /
   `summary.json.review_pending` report a dispatched review until the active objective identity changes.
7. **`WAITING_OBJECTIVE`** (`RunStatus`, `runner.RunStatus.WAITING_OBJECTIVE`). An objective is DONE when all its
   probes are true on fresh usable RAM. `abort_if` is rejected and cannot complete a goal. This includes `hold`:
   an already-true RAM probe completes before input;
   an unmet hold with only flag/var probes waits as `WAITING_OBJECTIVE` instead of counting dialog options as progress. A done
   objective never starts a new free-roaming decision: at the first safe scene boundary (`field.free` or an idle
   `dialog.end`) with a chooseable (or an empty) option set, `RunnerConfig.on_objective_done` decides: `hold` (default) keeps the run alive without input, advancing
   `hold_idle_frames` and sleeping `hold_poll_s` per iteration while it polls the file (bounded by
   `max_steps` / `deadline_s`); `pause` stops with `WAITING_OBJECTIVE`; `continue` atomically clears
   the completed ID + revision under the writer lock and keeps playing in the same run. Completion
   and `completion_cleared` history preserve the revision floor and prevent old-file resurrection.
   Queued/newer objectives survive; completion of an operator objective authorizes this transition
   while the manual-clear authority guard remains in force. The runner re-perceives after clearing
   before offering input. Neither objective changes nor completion in `continue` signal or restart
   the writer. Completion handoff waits during battle, menu and other busy scenes;
   review responses and hard safety limits still apply. No objective at all (no file, expired) is goal-less,
   not waiting. `AppConfig.on_objective_done`, `run.on_objective_done` in the
   run config, CLI `--on-objective-done`.
8. **CLI and exit codes.** `pokegent run --objective FILE [--objective-plan PLAN.json] [--on-objective-done
   pause|hold|continue]`; exit code 3 now also means `WAITING_OBJECTIVE` (summary: `stop_reason: waiting_objective`).
   `summary.json` gains `objective` (file, active objective, completed / aborted / done, `review_pending`,
   `reviews_requested`, `revisions_seen`, warnings, script status), `review_pending`, `objective_events` and, in
   `escalations`, `trigger` / `objective_id` / `revision` for reviews. `RunnerConfig` gains `on_objective_done`,
   `review_events`, `hold_idle_frames`, `hold_poll_s`; counters gain `objective_warnings`, `objective_reviews`,
   `objective_idle`.
9. **Scripted queue.** `ScriptedObjectiveSource` / `load_plan` (`objective-plan/1`, `docs/objective-file.md` section 9):
   a plan of objectives written through the same `ObjectiveStore` (history, `set_by: script`) as each previous one's
   probes succeed. For scripted league-run plans and tests; agent replaces it by writing the file. The store rejects
   scripted proposals over an active agent/operator objective, regardless of revision.
10. **Tracker additions** (`goals/tracker.py`): `abort_if` evaluation (`AbortSignal`, `aborts`, `aborted`, `done`,
    `completed`, `progressed`), `notify("stuck")` for the runner's counters, `STUCK_STREAK_ABORT = 3`.
    `ObjectiveStore.warning_count` / `warnings_since()`.

Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.


## 13. v1.4: bounded decider decision history

`run.decision_history` (default 3, integer 0..8; 0 disables) adds `recent_choices`
to the decider's context: its last choices, neutral executor outcomes and a bounded
mechanical diff against the next decision context. Automatic waits are not choices.
History clears on scene-family or objective id/revision changes and a decreasing
battle turn counter. Map changes retain the bounded history and its transition diffs,
including both directions of a cross-map revisit.
Builder-owned `recent_choices` wins; provider state budgets may trim or drop history.

The decider gets a copy; execution and choice validation use the paired original
frame, including a new original when an external pending goal refresh rebuilds it.
Options, fingerprints and fresh-RAM revalidation are unchanged. The original payload
guard runs first. A leak introduced only by optional history clears that history and
emits `runtime.degraded`; an original payload leak still blocks the decision.
This is bounded context, with no repetition ledger, ranking or recovery behavior.

## 14. v1.5: tactical instructions (removed in Phase 3)

Removed. `instructions.py`, `run.instructions` and the objective fields `instructions`, `abort_if` and `next_review_on` are gone; config keys and objective fields are rejected as removed. The agent writes its own tactics; the code never selects prompt text.

## 15. v1.6 (additive): read-only observers for the run viewer (`docs/addons.md`)

Nothing frozen changes. Observation points were added so a viewer can watch a run without being able to influence it:

1. `EventLog` / `MemoryEventLog.add_listener(fn)`: `fn(envelope)` runs after the event is stored, outside the log's lock; an
   exception in it is swallowed. Ids, order, redaction and the file are untouched.
2. `Perceiver.observers`: a list of `fn(perception)`, called with every perception the perceiver returns (runner and executor
   re-validation); exceptions are swallowed. `perceive` itself is unchanged (the body moved to `_perceive`).
3. `EmulatorActor.read_screen()` (framebuffer + the frame counter it belongs to, a read on the emulator thread) and
   `.frames_run` (a plain counter), both also on `ReadOnlyEmulatorView`. The single-writer rule is unchanged.
4. `EmuClock(idle_frames=False)`: a paced clock that runs frames only while a command needs them (default `True` = the old
   behaviour). With it the game advances exactly as in an unpaced run, only slower (`--realtime`).
5. `summary.json` gains `realtime` and, with the deprecated viewer alias, `viewer` (port and frame-read counters). `run_offline(viewer=...)`,
   `play_state(on_app=...)` and `open_emulator(fps=...)` are new optional parameters.

Rule: an observer never returns anything the runner uses.

## 16. v1.7 (additive, M4 fix): a press can be held until the game read it

Nothing above was renamed or removed. **Problem (M4 run 1, 9 of 80 uses of one item).** The game polls its
buttons once per main-loop iteration (`ReadKeys`), not once per frame, and an iteration right after a turn or after
a list scroll takes up to four frames. A fixed two-frame `Tap` can fall wholly between two polls: the press is never
read while the menu looks ready (controller function, cursor, tasks all say "input"), so no scene evidence could
detect it and the hash did not change.

**Contract.** `Tap` gains two optional fields, `until` (`Callable[[Observation], bool] | None`, default `None`) and
`max_hold_frames` (default 8). With `until` set the executor holds the button for `hold_frames`, asks `until` on a
fresh observation, and keeps holding one frame at a time (the button never comes up between chunks) until it holds or
`max_hold_frames` is reached, then runs `wait_frames` released. It is still ONE step and ONE input: `InputStep.hold_frames`
records the frames the button was down, an `atomic` option stays atomic, `max_inputs` counts one. A predicate that
raises stops the hold. `until=None` (every existing step) behaves exactly as before. The predicate must ask the game
(`gMain.heldKeys`, `ui.main.held_keys`), never the clock; the battle family's `macros.press` is the reference use.

Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.

## 17. v1.8 (additive, endgame gaps): an invalid core can carry a claim, the cleared game is an event

Nothing above was renamed or removed. **Problem (the runtime contracts, the "reboot gap").** After the
credits the game soft-resets and the save-block pointers are cleared, so the core reader may be invalid while the
copyright, intro, title and main menu are on screen, although the UI block (`gMain.callback2`, the task list) is valid.
Rule 1 of section 4 and invariants 3/8 above made that a `SNAPSHOT_INVALID` pause with the game cleared and saved.

**Contract.**

1. A detector may declare `accepts_invalid_core = True` (optional attribute, default absent; only the boolean `True`
   counts, `state.scenes.accepts_invalid_core`). On an INVALID core snapshot whose `snapshot.ui` is valid the registry
   runs EXACTLY those detectors, ranks them by the usual rules among themselves, and returns the winning scene with
   `SceneResult.core_invalid = True` (new field, default `False`; `evidence["core_read_errors"]` names the core's read errors).
   No claim, ambiguity, a low-confidence claim or an invalid/absent UI block stays `system.invalid`. A usable snapshot is
   never marked. The only detectors that declare it are the logo, title and main-menu detectors of `system_flow`
   (they need nothing but the UI block); Hall of Fame, credits, evolution and PC still need a valid core.
2. `state.scenes.readable(snapshot, scene)` = `snapshot.usable or scene.core_invalid` is the one rule that replaces
   "snapshot.usable" at five places: `Perceiver._perceive` (kind `INVALID` otherwise), the executor's re-validation (invariant 3:
   "usable" now reads "readable"), its step boundaries, its postcondition wait and `before_hash` / `after_hash`. Everything else
   of invariants 3 and 8 holds unchanged: no input on an invalid snapshot that no accepting detector claims, a FRESH
   snapshot is perceived again before every input (and must again be `readable`, same scene, same fingerprint), no blind input.
   Objective probes still read only usable snapshots (`Runner._objective_observe`).
3. `EventType.PROGRESS_GAME_CLEARED` (existed, never emitted) is emitted by the runner ONCE per run, at the first perception
   whose usable snapshot has `FLAG_SYS_GAME_CLEAR` set (`Runner(game_clear=...)`, an object with `observe(snapshot) -> bool`;
   `build_runner` passes `FamilyBundle.plugins["system_flow"].game_clear_watch`). Details: `flag`, `snapshot_seq`, `iteration`,
   `at_start` (the run began on an already cleared game). `RunSummary.game_cleared` and `summary.json` `"game_cleared"` mirror it.
   `clear` from the objective block).
4. `system.quest_log_playback` stays DETECTED by the dialog family. With the `system_flow` family registered and its option
   `quest_log: skip` (default), `register_all` hands the BUILDER over (`dialog.register(quest_log_builder=False)`, manifest and
   ownership report say `detector dialog / builder system_flow`); `quest_log: off`, a missing `system_flow` or
   `dialog_system_scenes=False` keep the old behaviour.

Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.

## 18. v1.9 (additive): party move proof for field-tool objectives

The objective probe union gains `{"kind": "party_knows_move", "move_id": 1..354}`. Validation
resolves it without strategic interpretation, and `ObjectiveTracker` evaluates it from a fresh,
usable party snapshot by requiring at least one party member to contain that move id. This is the
proof used after HM02 teaching (`move_id: 19`, Fly); it does not prove species compatibility or
that a field move is legal on the current map. Those remain field-family facts and executor gates.

Validate changes with `ruff check .`, `python -m compileall pokegent addons`, and
`python tests/games/gen3/test_imports.py`.

## 19. v1.10 (additive): team and wild-encounter context

The `/briefing` payload exposes RAM facts needed to understand team capacity: `pokedex.owned` /
`pokedex.seen`, `party_size` / `party_free_slots`, and `pc.stored_count` plus `pc.stored_mons`
(`species` and ROM-derived `level`). An unreadable RAM block is represented as `unknown`. The
current map also exposes `wild_encounters` with ROM method, species, level range, rate, and
Pokédex `owned` / `seen` / `new` fields (`new` means not owned; `unseen` means not seen). Grass and Surf field options carry the same encounter
facts in their criteria; rendered species lists are compact and append `+N more` when capped.
These are facts only: no team planner, advice, or usefulness ranking is added. Briefing trimming
removes low-value events/side/bag detail before stored-PC rows and records `pc.stored_mons` in
`diagnostics.trimmed` whenever those rows are capped or reduced.

## 20. v1.11 (additive): a decision-blocked route is a route step

Some edges cannot be unblocked by any capability: the Mt Moon B2F fossil choice is a one-time
decision, and no bag item, badge or flag makes its edge available. A route that needs it used to
be reported as an unreachable barrier, so no option was advertised as the step toward it and the
decider had no route fact to follow.

`NavigationPlan` gains `blocked_choice_step` (default `None`): the stable key of the first edge
whose unmet requirements are decision requirements (`CHOICE_REQUIREMENTS`, currently `fossil`).
It is set only when **every** unmet requirement on the whole route is such a decision requirement;
a route that also needs a capability (a fossil followed by a Cut tree, or one edge needing both)
stays capability-blocked with `None`. When both planner searches find no route at all (a target
past the fossils, or a world search that exceeds its budget), the planner runs one bounded search
over the current map and, if the nearest decision-blocked edge is reachable, returns a plan with
no route, the decision label in `unmet`, `blocked_choice_step`, and a `blocked_route_fact` naming
the decision ("no route known to …; next decision: fossil at …"). No route is invented: the step
is the only known next decision.

The adapter advertises the option that performs that edge with `route_relation: next route step`,
`route_step` / `next_route_step`. The no-route plan also carries `route_needs` and
`blocked_choice` on that option; on a route-present plan those two facts are not set and the
ordinary `route_blocked` / `route_cost` facts apply. The no-route step does **not** carry
`serves_goal` or `on_route`: no route to the objective is known, so the frame must not claim the
decision serves it (the target may be unrelated or unreachable through it). Every other option
keeps the ordinary `no_route` / `unreachable` facts. This changes no executor, no legality and no
requirement: the edge is still unmet, the step is simply visible as the next decision. The option
keeps its own facts, so a fossil option stays `irreversible` with `risk: high`; handing an
irreversible pick to the agent is the hybrid irreversible gate, not part of this contract.

### Opt-in objective successor fallback

With `objectives.fallback: dex` and `run.on_objective_done: continue`, the next decision after
fresh safe completion uses the next configured queue entry or an eligible private-ROM-backed dex
goal. Completion history and revision-floor/CAS rules above still apply. Pending/rejected queues,
active foreign objectives and expiry cannot authorize fallback takeover. Missing/unknown facts,
unresolved heal/restock prerequisites and no supported candidates diagnose and SAFE_PAUSE; missing
private encounter data is a setup error. This restores no story dataset or tactical/review fields.
See [the complete opt-in contract](objective-file.md#9-opt-in-successor-fallback).

### Authored story fallback (bounded synthetic slice)

`objectives.fallback: story` requires explicit `run.on_objective_done: continue`
in the file and after overrides. Its separate fixed external artifact holds authored
order/required policy and validated objective templates, with declared provenance;
it changes none of the exact-seven setup schemas. Current-snapshot unknown proofs
cannot complete a node. Session retains queue/manual/expiry/durable-history/CAS
authority; the existing fallback seam selects a prerequisite before its downstream
milestone. Required completion produces `story_complete` SAFE_PAUSE at the existing
safe boundary, rechecking the empty revision and successor after callbacks. No
terminal hold, silent Dex switch, new input or navigation policy is introduced.
See [the schema and limits](objective-file.md#10-opt-in-authored-story-source-synthetic-engine-slice).

Optional `story_facts` are read-only state/briefing data from the exact offered
frame's snapshot, also during manual goals. They expose authored-order challenge
and proven/unmet/unknown prerequisite evidence, with declared provenance and no
readiness beyond prerequisites. The bounded unit is leak-checked before attachment,
omitted on failure, and dropped whole under provider/briefing budget pressure.
It does not alter objective/history serialization, action fingerprints or frame
pairing. Pending goal rebuilds reuse the same snapshot; briefing stays unchanged.

Story admission at the objective-session factory binds declared provenance to the
selected verified ROM image, pinned exact-seven setup, active index and fresh cache
file hashes, including prebuilt story sessions. Missing identity fails closed with
a redacted setup error and no automatic regeneration. Loader-only structural use
retains its API. `objectives.story_path` selects a sibling private artifact without
changing seven-file setup ownership. Current facts, completion, queue/CAS authority
and frame pairing retain their existing same-snapshot contracts.

Explicit offline `rom-data story` generation produces only a conditional partial
candidate for independent source/data review. The exact negated return-branch
premise remains attached to all scoped effects and candidate receipts; it is never
observed current RAM or readiness. The unrestricted incomplete interpretation and
unsupported branch remain visible. Three exact spatial exceptions remain unresolved.
No static derivation completes an objective: both persistent milestones still need
current usable same-snapshot proof through the existing session/runner boundary.
