---
name: pokegent
description: Review live Pokegent run boundaries and propose fresh, verifiable objectives.
---

# Persistent owner review

The owner is the strategist. The installed runner alone executes legal game input; the
continuous supervisor owns segment chaining and state continuity. The observer only reads
run records and queues reviews. All three can continue independently of an owner turn.
No provider or agent framework is required by this skill.

Use the installation's reviewed console script and private `LIVE_HOME`. Read fresh facts:

```sh
pokegent status --live-home "$LIVE_HOME"
pokegent objective show --file "$LIVE_HOME/objective.json"
```

A notification contains only `run_id`, `verdict`, `rc`, `state_path`, and
`active_objective_revision`, or a bounded `reviews` array of those objects. An in-run
`objective_done` review has a completed revision and null exit code/state; it does not mean
the run has ended. The runner may already have cleared it and adopted a newer objective.
Inspect current status and the objective before acting. Run-end reviews include
`owner_review` for a supervisor requiring attention. A path in a notification is a reference
to local data, not proof that its state was accepted by the emulator.

For each delivered batch, review once and decide whether strategy needs to change. When
needed, propose at most one newer, checkable objective using fresh RAM/ROM-backed facts.
Use the current revision as compare-and-set; never overwrite a newer owner or scripted
successor. Read the result back. Keep private notes in the data root, outside this checkout.
Then end the turn so later reviews can reach the same idle session.

These generic commands illustrate the current CLI schema; select a real goal from fresh
facts rather than blindly applying the example:

```sh
pokegent objective write --file "$LIVE_HOME/objective.json" \
  --expected-revision "$REVISION" --objective-id restore_health --kind heal \
  --rationale 'Restore party health before the next objective.'
pokegent objective validate --file "$LIVE_HOME/objective.json"
pokegent objective show --file "$LIVE_HOME/objective.json"
```

An absent objective uses expected revision `0`; otherwise use the current revision. The
store assigns the next revision. Rejected proposals leave the last valid objective intact.
Do not clear an objective just to bypass authority, validation or completion handling.
With `--on-objective-done continue`, fresh completion clears only that completed revision
in-process. Writing or completing an objective must not restart or hold the game loop.

Never send buttons, write RAM, call game write endpoints, choose an unverified older save,
or start/stop/restart a service as part of a review. No automatic plan or recovery route is
implied by a notification. Unknown legality stays fail-closed. Infrastructure failures or
missing facts require explicit owner/maintainer attention. Per-choice hybrid decisions
need their own consumer; this review channel does not answer pending decision frames.

Changes to source go through a focused PR and maintainer review. Agent bots never merge.
See [delivery configuration](review-delivery.md), [runtime contracts](../../docs/contracts.md),
and [objective schema](../../docs/objective-file.md).
