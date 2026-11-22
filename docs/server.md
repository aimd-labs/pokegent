# External decider in five minutes

`agent` is the loopback bridge decider: any agent or human can steer the normal router loop. The
endpoint binds only to loopback and exposes the same guarded decision payload
that the System One HTTP provider receives.

Start a run (use a fresh output directory):

```bash
source /path/to/your/venv/bin/activate
pokegent run --state path/to/start.state --rom "$FIRERED_ROM_PATH" \
  --decider agent \
  --max-steps 5 --out runs/agent-demo
```

Use the [connect-your-agent guide](connect-your-agent.md#agent-mode-inspect-choose-verify)
and its executable example to inspect a frame and submit its exact `frame_id` with an offered
`option_id`. The default bridge port is 19760. `pokegent step --port 19760` can inspect a frame.
With `choose --frame-id <id>`, the CLI sends that inspected token without a `GET` and prints
the `/choose` acceptance response: the choice is queued, not a verified action. Without
`--frame-id`, it keeps the legacy refetch behavior and prints the subsequent fetched frame.
The older unconditional refetch warning from #61 applies only when `--frame-id` is omitted.
Execution results arrive in subsequent feedback.

When the runner advances a dialog on the agent's behalf during any action, that action's feedback
item also has `dialog_text`, a list containing each decoded page captured at the executor's
observation boundaries. An undecodable page is reported as `"unreadable"`; the runner never
substitutes text from unproven or stale RAM. The same action details, including `dialog_text`, are
written to `events.jsonl`.

An agent can use the same loop with `GET /frame`, `POST /choose` with
`{"frame_id":"...", "option_id":"..."}`, and `GET /status`. A stale frame
returns HTTP 409. In `agent` mode the runner waits for the agent's choice; it never chooses an
option on its own. In `hybrid` mode each escalated agent handoff (including unknown-scene and
no-progress recovery) is bounded by `hybrid.agent_handoff_timeout_s` (default 300 seconds).
On expiry the runner emits `decision.agent.expired`, stops waiting, and pauses with
`SAFE_PAUSE`; it does not reuse the rejected System One choice or start another handoff.
Hybrid falls back to the plain agent decider when System One is disabled or unconfigured; that
fallback keeps the unchanged agent-mode wait and is not bounded by this key.
`/status` reports `waiting_for_choice` while it waits.

Each bounded hybrid handoff carries an agent-only top-level `handoff` record on the served
frame. It is not metadata and never enters the System One payload:

```json
"handoff": {
  "schema": "handoff/1",
  "controller": "AGENT_PENDING",
  "ownership": "escalated",
  "decision_id": "dec-...",
  "frame_id": "external-...",
  "snapshot": {"seq": 123, "fingerprint": "sha256:..."},
  "objective": {"id": "...", "revision": 4},
  "option_set_hash": "sha256:...",
  "option_scope": "selectable_union",
  "escalation_reason": "low_confidence",
  "deadline": {"policy": "safe_pause", "timeout_s": 300.0, "remaining_s": 299.7}
}
```

`ownership` is informational (`escalated` or the runner's `recovery_window`); it grants no
objective authority. `option_set_hash` is a canonical digest of the option ids and rendered
criteria in both the normal view and the `?all=1` extended view, so it identifies the full
selectable union and is independent of option order. `objective.revision` is `null` when the
frame was built without a known revision, and `objective.id` is `null` when goal-less.
`deadline.remaining_s` is generated from the one private monotonic deadline; a refresh rebuilds
the record with the new `frame_id`, objective revision, option hash and remaining time without
extending the wait, and a choice posted with the previous token returns HTTP 409. On expiry the
`decision.agent.expired` event stays keyed to the decision id and reason; it does not restate
snapshot/objective identity, so use the record on the last served frame for token identity.
Use `POST /interrupt` to request interruption of an active macro or interruptible wait.

`GET /frame` long-polls for the next frame for up to 30 seconds by default. Clients must set their
HTTP timeout above that value, or pass `?wait=<seconds>` to use a shorter wait (0 through 30).
Invalid wait values return HTTP 400. `pokegent step --wait <seconds>` passes the same shorter wait;
without it, the CLI retains the 30-second default.

While `--on-objective-done hold` is holding after completion, `/status` and the no-frame response
from `step` retain their existing fields and add `waiting_for_objective: true` plus the completed
`objective_id`.

The endpoint is unauthenticated and intentionally loopback-only. Do not expose
it directly to a network; use a controlled local agent or an authenticated
tunnel if needed.

## Planning loop

The planner can read a compact, ROM/RAM-backed view without waiting for a decision:

```bash
pokegent step --port 19760 briefing
pokegent step --port 19760 objective set objective.json
```

The usual loop is: read `/briefing`, set one validated `objective/1`, let the decider/agent play,
then re-brief after a milestone, faint, loss, or other review event. `GET /objective` reports the
active objective; `DELETE /objective` returns the run to goal-less play. These planning endpoints
are available on the same loopback bridge in agent, hybrid, and enabled System One runs.

## Screenshot

External and hybrid runs serve the latest game-only emulator frame as a PNG:

```bash
pokegent step --port 19760 screenshot --out current.png
```

This is equivalent to `GET /screenshot`; `?scale=1`, `?scale=2`, `?scale=3`,
or `?scale=4` selects nearest-neighbour output. Scale 1 is 240x160. Frames
are captured by the core frame source and requests serve the last completed
capture, so a screenshot request never pauses emulation. This works with both
optional addons disabled. No overlay or internal decision data is included.

## System One and hybrid modes

System One is optional: use `--decider system1` for it directly, or `--decider hybrid` to keep
an agent override path available.

`--decider hybrid` uses System One for every frame by default and keeps this
same `/frame`, `/choose`, `/status`, and `pokegent step` interface on one
loopback server. A served hybrid frame includes `system1.option_id`,
`system1.probabilities`, and `system1.confidence`.

Set the mode and policy in `pokegent.yaml`:

```yaml
decider: hybrid
hybrid:
  escalate_below_confidence: 0.35
  override_window_s: 0
```

`escalate_below_confidence` may also be a mapping such as
`{default: 0.35, battle: 0.5}`. The matching `scene_id` threshold is used; unknown scenes use
`default`, and an omitted `default` uses `0.35`.

Low confidence, a System One error/breaker, the runner's stuck detector, or a
missing System One confidence always escalates the frame to the agent.
A choice posted before execution wins. An override is possible only on an
escalated frame, unless `override_window_s > 0` enables a temporary override
window for otherwise non-escalated frames. A bounded hybrid escalation waits up to
`hybrid.agent_handoff_timeout_s`; the existing System One suggestion is never applied because
the agent was slow.
When a non-escalated override window expires, the decision is recorded as
`decided_by: system1`. Completion events identify `system1` or `agent`.

A System One pick that the frame's facts mark as off the objective is not followed when the
frame names exactly one next route step and the pick is a navigation target (a warp, edge, fly,
door, ladder or tile walk): the hybrid decider replaces the pick with that step and does not call
the agent lane (`vetoed_by: route_step` in the status). The replacement is the planner's step,
not a System One answer, so it carries no confidence; the served frame keeps System One's actual
pick, and the served metadata (`vetoed_by`, `vetoed_option`) and completion event keep its
`option_id` and `confidence`. An item, bicycle, NPC, heal or trainer pick is not a navigation
choice and keeps the normal confidence gate even when it is off route. A frame that names more
than one candidate route step is ambiguous and escalates with
`escalation_reason: multiple_route_steps`. An on-route pick and a frame without an objective are
unchanged.

An option marked irreversible (the `irreversible` field, or `facts.irreversible` for families
that carry the evidence there) is never applied on a System One pick: the frame always escalates
to the agent, whatever the option's confidence and the scene threshold
(`escalation_reason: irreversible_option`). This is checked against the post-veto choice, so a
route-step replacement that is itself irreversible escalates instead of being applied; a single
route step that is only high risk (a trainer battle on the way) is not applied either and
escalates with `escalation_reason: high_risk_route_step`. An option that is not marked
irreversible keeps the normal confidence gate.
