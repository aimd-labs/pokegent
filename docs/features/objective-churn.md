# Objective churn guard

`run.objective_churn_limit` (default `0`, off) is an opt-in diagnostic for a run whose
objective file keeps changing while the emulator is not moving. It uses the existing
`progress.stuck` event; no event type, objective schema, input or review policy is added.

- `0` leaves the tracker byte-for-byte on its previous behavior.
- A positive value is an integer `>= 2`: the number of real objective id/revision changes
  that may occur before one `progress.stuck` is emitted.

## What counts

A *real change* is a different objective id or revision applied by `ObjectiveSession`
(or `ObjectiveTracker.set_objective`). Repeating an update under the same objective is
not a change. The guard compares only `StateSnapshot.emulator_frame`; it never infers
progress from `snapshot_seq`, `goal_changed` events or wall-clock time.

The first usable snapshot establishes a frame baseline. Each later real change increments
a bounded counter. A change to `emulator_frame` (including a save-state reset) starts a
fresh incident with a new baseline. An unusable snapshot or a usable snapshot without an
`emulator_frame` cannot prove a freeze: it clears the incident, and the next usable frame
starts over.

## Event

When the counter reaches the limit on a usable snapshot whose frame equals the baseline,
one `progress.stuck` is emitted with `source="objective"` and details:

| key | meaning |
|---|---|
| `objective_id`, `revision` | the active objective identity |
| `reason` | `objective_churn` |
| `churn_changes` | real changes observed in this incident (integer) |
| `emulator_frame` | the frozen frame (integer) |

The incident is reported once. Progress (`emulator_frame` advance) re-arms a new incident;
the active objective is never cleared or replaced by this guard. Review requests still
come only from `next_review_on: ["stuck"]`; the guard adds no Runner action authority.

## Configuration

```yaml
run:
  objective_churn_limit: 3
```

Values `1`, booleans, negative numbers, non-integers and `null` are rejected. The setting
flows through `AppConfig` -> `make_objective_session` -> `ObjectiveSession` ->
`ObjectiveTracker`.
