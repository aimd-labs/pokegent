# Exit-cycle guard

`runner.exit_cycle_guard` (default `false`) is an opt-in diagnostic for a three-action
round trip across two maps. It uses the existing `progress.stuck` event and the normal
no-progress escalation path; no event type, schema, action, input or agent authority is
added.

- `false` leaves the legacy progress monitor byte-for-byte unchanged.
- `true` enables a separate bounded ring of nine verified decisions. Only three identical
  consecutive blocks of three can report a cycle.

## What counts

Each entry holds the option id, the full semantic progress signature (position, party, bag,
flags) and the exact map identity `(group, number)`. All three blocks must be identical, so
a new frontier or changed durable state breaks the evidence. The observation after the
ninth action must also return to the start of the repeated block, signature and map
included; a ninth action that lands somewhere new keeps the run going. A repeated action id
alone is never enough: the block must contain three distinct ids, exactly two
`warp:`/`edge:` exits, one `strength:` action, and exactly two map identities. The
pre-action maps must follow the real two-floor shape: first map different from the second,
with the second and third equal (`A -> B -> B`); the post-action closure then proves the
return to `A`. A wrong transition such as `A -> A -> B` would imply that strength
changed maps between the first two actions; that is not this shape.

A map key is supplied only from a usable snapshot whose map identity is ROM-verified and
whose player position is consistent. Anything less clears the new ring and makes the guard
ineligible for that observation.

Only an applied action with `verified_outcome is True` enters the ring. Interrupted, failed,
unverified or noneligible actions clear it. An unreadable or unusable map clears only the
new ring and makes this detector ineligible. Ordinary position-progress resets retain the
ring, like the legacy loop evidence; `reset_window(clear_loop=True)`, a goal revision or a
recovery rearm clears it.

## Event

When the ninth entry completes the third block, the next observation reports through the
existing no-progress path with:

```text
exit_cycle:entries=9,actions=3,maps=2
```

No objective is changed automatically and the run stops for operator or supervisor review
exactly as any other `no_progress` verdict.

## Configuration

```yaml
runner:
  exit_cycle_guard: true
```

Non-boolean values are rejected.
