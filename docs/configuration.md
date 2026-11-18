# Configuration

See [System One endpoints and safe model switching](system-one.md) for protocols,
authentication, timeouts and terminal provider rejection.

Copy `pokegent.example.yaml` to `pokegent.yaml`. The agent loads that file when
present, then applies environment variables, then explicit CLI flags. The real
`pokegent.yaml` is ignored by git. Pass another file with `--config PATH`.

The main keys are `rom_path`, `decider` (`agent`, `system1`, `hybrid`, `first`, or
`forced`). The one-release `external` alias runs `agent` with a deprecation warning.
`system1.base_url`, `system1.api_key` or `system1.api_key_env`, optional
`system1.model`, `system1.request_model` and `system1.timeouts`, `external.port` (19700–19799) and
and `hybrid.escalate_below_confidence` (default `0.35`, or a mapping of `scene_id` to thresholds
with optional `default`; unknown scenes use `default`, or `0.35` when omitted),
`hybrid.override_window_s` (default `0`), and `hybrid.force_agent_after_repeats` (positive integer,
default `2`). After that many consecutive executed System One choices of the same option on the
same scene and state fingerprint without verified state change, the next decision bypasses System
One and waits for the agent (`decision.agent.requested`, reason `repeated_option`). A new state or
option, verified progress, an agent choice, or an intervening forced action resets the streak.
A successful interaction postcondition alone does not establish state change. These choices still
pass normal frame and executor validation. `battle.opponent_knowledge` (`guide` by default or
`fair_play`), `field.menus`, and `output.run_dir`.

`run.on_objective_done` accepts `hold` (default: stay alive without input and poll for a new
objective), `pause` (stop with `WAITING_OBJECTIVE`, exit 3), or `continue`. With `continue`, fresh
completion at a safe boundary atomically clears only the matching objective ID and revision,
including an operator objective, and play continues in the same process. A newer or queued
objective is preserved. Set it in YAML or with `--on-objective-done continue`; see
[objective lifecycle](objective-file.md#8-runner-lifecycle-and-configuration).

Hybrid mode offers one bounded agent recovery window on a no-progress verdict when
`hybrid.escalate_on_no_progress` is true (default). The next action-bearing decision bypasses
System One and emits `decision.agent.requested` with `escalation_reason=no_progress`; subsequent
decisions remain agent-owned until the executor reports durable progress (`ActionResult.progressed
is True`). Position-only movement, a changed monitor signature, and a verified interaction
postcondition do not end or rearm the window. After another `runner.no_progress_limit`
action-bearing decisions without progress, or an earlier existing guard verdict, the run enters
SAFE_PAUSE (exit 1). Verified progress rearms the single recovery window. Frame legality, executor
validation and invalid/unsupported-state pauses still apply throughout. Disabling this setting
retains the original no-progress stop. Non-hybrid modes keep their existing behavior.
There is no automatic game-input recovery ladder.

With `hybrid.escalate_unknown_scene: true` (the hybrid default), an unknown scene that persists
past the bounded settle retries is offered directly to the agent with
`escalation_reason=unknown_scene`, the current decision frame, and the existing `/screenshot`
endpoint. System One is bypassed. The generic set contains a 30-frame wait and, with valid UI
evidence and no reported input lock, one short B/UP/DOWN/LEFT/RIGHT tap. No game-specific effects
are inferred. A/START and other confirmation inputs are withheld: an unknown screen requiring A
may still end in bounded SAFE_PAUSE and needs supervisor review.

Unknown-scene handling shares the no-progress agent window: at most `runner.no_progress_limit`
agent decisions without executor-verified durable progress, including wait-only decisions.
Changing options, positions, or UI screens does not renew the window. The unknown-scene opt-in is
independent of `hybrid.escalate_on_no_progress`; disabling it restores the original escalation.
Other decider modes are unchanged. Every primitive uses fresh scene/core/UI fingerprint and
option revalidation and a bounded RAM/frame postcondition; verifying a tap/wait does not claim
semantic progress. Invalid or unsupported state retains its normal pause path.

Saved-state validation checks the ROM header and RAM invariants, independently of the last scene
classification. A healthy save in `scene.unknown` is resumable; unknown does not excuse corrupt RAM
or a missing avatar outside the established pre-field callback allowlist.

Old `recovery:`, `objectives:` and `runner.busy_nudge_after`
keys are rejected with an error, never silently ignored.

Environment overrides include `POKEGENT_ROM_PATH`, `POKEGENT_DECIDER`,
`SYSTEM1_BASE_URL`, `SYSTEM1_API_KEY`, `SYSTEM1_MODEL`, `SYSTEM1_MODEL_ID`,
`SYSTEM1_REQUEST_MODEL`, `SYSTEM1_PROTOCOL`, `SYSTEM1_AUTH_HEADER`, `SYSTEM1_TIMEOUT`,
`POKEGENT_EXTERNAL_PORT`, `POKEGENT_FIELD_MENU`,
`POKEGENT_HYBRID_ESCALATE_BELOW_CONFIDENCE`,
and `POKEGENT_HYBRID_OVERRIDE_WINDOW_S`. API keys are never printed by the configuration layer;
prefer `api_key_env` so the secret is not stored in the file.

`SYSTEM1_REQUEST_MODEL` overrides `system1.request_model` and is sent as the HTTP JSON
`model` selector. If neither is set, the field is omitted; an empty environment value does
not override a YAML value. Endpoints that require a selector need an explicit supported
value. No endpoint or model default is inferred. `SYSTEM1_MODEL_ID` (or `SYSTEM1_MODEL`)
sets `system1.model`, the expected response ID for telemetry and optional strict pinning;
it is never reused as the request selector. The legacy `system_one` section also supports
`request_model`, including `${SYSTEM1_REQUEST_MODEL:-}` expansion. See
[provider configuration](provider-integration.md#configuration).

Compatible TM/HM teaching options are always available; legal game actions are not hidden behind
configuration or objective relevance. `POKEGENT_FIELD_MENU` still controls non-teaching field-item
options. Teaching verifies the learned move and consumes an ordinary TM while retaining an HM.

`hybrid.escalation_timeout_s` and `POKEGENT_HYBRID_ESCALATION_TIMEOUT_S` were removed;
`external.timeout`, `POKEGENT_EXTERNAL_TIMEOUT`, and `--external-timeout` were removed because
agent choices wait indefinitely.
supplying either is an error because escalated decisions wait for the agent indefinitely.

## Graceful stop

For continuous segment chaining, state continuity and process-manager configuration, see
[the live supervisor](live-supervisor.md). Its environment configuration does not replace this
per-run YAML schema.

`run.graceful_stop_timeout_s` is the hard graceful-stop budget (default `10`). SIGINT, SIGTERM, or
creation of `run.restart_file` cancels an in-flight System One HTTP request or external-decider
wait, then stops before the next input and atomically writes the validated `final.state`. A restart
marker left over from an earlier process is removed at startup and ignored.

## Cold-boot new game

`pokegent run --new-game` boots the ROM in-process and drives the title/new-game flow without a
state file. Only explicitly identified pre-game scenes are accepted before FireRed creates the
avatar; ordinary `--state` runs retain the strict valid-avatar startup gate. Gender defaults to boy,
and the name screens choose the game's first preset rather than typing a custom name.

Checkpoint and final-save validation accepts an absent avatar only when the core reader verifies
a recognized pre-field callback and finds no active player object anywhere. In that case the
uncreated object's coordinates are not compared with the saved position. Once the indexed object
is an active player, coordinate agreement is required even during the intro; invalid indices,
wrong indexed players, other callbacks, ROM headers and all other reader checks remain enforced.

```yaml
new_game:
  gender: male       # male or female
```

If the run has not reached its normal save path by the deadline, the watchdog queues the same
validated save as one exclusive actor transaction, allows it at most one additional second, then
allows up to 0.5 seconds to flush and close `events.jsonl` before exiting 124.
A second SIGINT/SIGTERM takes that bounded save path immediately and exits 130. The process may
therefore exit before `summary.json` is written on either hard path; `final.state` is the recovery
artifact. Before the coordinator starts and after it closes, the process uses OS-default signal
termination so another signal cannot be consumed without an active watchdog.

Run exit codes are: 0 normal/requested stop, 1 blocked, 2 usage, 3 paused, 4 environment, 124
graceful-stop deadline, and 130 forced interrupt.

## Local game data

Set `POKEGENT_LOCAL_DATA_DIR` before startup to load validated game-data files outside the checkout.
`POKEGENT_MAP_GROUPS` optionally overrides the map index. See [local-data.md](local-data.md) for schemas,
missing-file behavior and the setup limitation until automatic local generation exists.
