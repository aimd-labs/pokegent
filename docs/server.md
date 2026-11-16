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

In another shell, inspect the current decision:

```bash
pokegent step --port 19760
```

The response contains a `frame_id`, the scene, objective and public state
facts, instructions/question, and each option's readable criterion. Pick one
by its option id; POST `/choose` returns `202 Accepted` after handing the
choice to the runner. The next frame's `feedback` reports the
applied/failed/interrupted outcome and verification reason:

```bash
pokegent step --port 19760 choose <option_id>
```

When the runner advances a dialog on the agent's behalf during any action, that action's feedback
item also has `dialog_text`, a list containing each decoded page captured at the executor's
observation boundaries. An undecodable page is reported as `"unreadable"`; the runner never
substitutes text from unproven or stale RAM. The same action details, including `dialog_text`, are
written to `events.jsonl`.

An agent can use the same loop with `GET /frame`, `POST /choose` with
`{"frame_id":"...", "option_id":"..."}`, and `GET /status`. A stale frame
returns HTTP 409. The runner waits indefinitely for the agent's choice; it never chooses an
option on its own. `/status` reports `waiting_for_choice` while it waits. Use `/interrupt`,

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
window for otherwise non-escalated frames. An escalation waits indefinitely for
the agent's choice; the existing System One suggestion is never applied because
the agent was slow.
When a non-escalated override window expires, the decision is recorded as
`decided_by: system1`. Completion events identify `system1` or `agent`.
