# The objective file: what the agent hands the game

Code: `pokegent/goals/`, `pokegent/loop.py`, `pokegent/app.py`, and
`pokegent/server.py`. The agent is the strategist: it writes one atomic, verifiable objective at a
time, or uses `ObjectiveStore.propose()`. The game supplies facts and options; it does not invent a
strategic choice. A missing or expired objective leaves the run goal-less.

The JSON examples below use the real `objective/1` model. Unknown fields are rejected; in particular,
the Phase 3 fields removed from that model are rejected loudly.

## 1. Where and how

The loopback bridge exposes the same store: `GET /objective` reads the active objective, `POST
/objective` proposes one through `ObjectiveStore.propose()`, and `DELETE /objective?expected_revision=N&set_by=...`
clears it with compare-and-set. The CLI equivalents are `pokegent step briefing` and
`pokegent step objective get|set|clear`.

| Thing | Path | Writer |
|---|---|---|
| active objective | `<run dir>/objective.json` | The agent, an operator, or a configured scripted plan |
| history | `<file>.history.jsonl` | the store |
| writer lock | `<file>.lock` | the store |

`ObjectiveStore.current()` polls the file. A corrupt, oversized, stale, vanished, or semantically
invalid file keeps the last valid objective and records a typed warning; it does not raise through the
runner. A valid newer revision is adopted. A revision must be greater than the active revision.

## 2. Schema `objective/1`

```json
{
  "schema_version": "objective/1",
  "objective_id": "reach_league_lobby",
  "revision": 1,
  "kind": "reach",
  "target": {"map": "IndigoPlateau_PokemonCenter_1F"},
  "success_probes": [],
  "rationale": "Eight badges. The lobby has the nurse and the door to the league.",
  "set_by": "agent",
  "expires_at": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `"objective/1"` | optional on write; written back by the model |
| `objective_id` | `^[a-z][a-z0-9_]{0,47}$` | stable semantic id; becomes the frame `goal_id` |
| `revision` | integer >= 1 | strictly greater than the active revision |
| `kind` | `reach`, `talk`, `fight`, `collect`, `shop`, `heal`, `dex`, `train`, `hold`, `team-prep` | objective behavior |
| `target` | object | names in the agent's vocabulary; see section 3 |
| `success_probes` | list | RAM facts proving completion; some targets derive one when omitted |
| `rationale` | string <= 600 characters | model-visible prose |
| `forbid` | list, optional | hard executor-class constraints: `pc`, `storage`, `fly`, `shop`, `tm`, `sell` |
| `set_by` | string | writer identity; defaults to `agent` in the public model and CLI (legacy `agent` is accepted) |
| `expires_at` | ISO-8601 datetime or null | after this instant the run is goal-less |

The model is frozen and rejects unknown keys. Only `forbid` is a hard option constraint. Prohibition
language in `rationale` is prose and does not create a constraint. `forbid` must be a list, not a bare
string. `pc` and `storage` are distinct executor classes.

### Removed in Phase 3

`instructions`, `abort_if`, `next_review_on`, `run.on_objective_review`,
`run.objective_stuck_after`, and `runner.review_events` were removed. Supplying any of these fields or
configuration keys is rejected loudly.

## 3. Kinds and targets

| `kind` | allowed target | derived success probe when probes are empty |
|---|---|---|
| `reach` | `map`, optionally `warp` or `x`+`y` | map arrival, or the target tile; a warp requires an explicit destination probe |
| `talk` | `map` + `npc` | none; provide a flag, var, or other probe |
| `fight` | `map` + `npc` | none; provide a probe |
| `collect` | exactly one of `item` or gift-Pokémon `species`, optional `map` and `quantity` | item quantity, or Pokédex ownership for a gift species |
| `shop` | required `item`, optional `map` and `quantity` | item quantity |
| `heal` | optional `map` | every non-egg party member has full HP, no major status, and every known move at max PP (ROM base PP plus PP Ups) |
| `dex` | exactly one of `species` or caught `count`, optional `map` | dex ownership or count |
| `train` | optional `map` | none; provide `party_level` and/or `party_healthy` |
| `hold` | none | none |
| `team-prep` | none | none; provide a `party_species` probe |

Map names are pret names; case, `MAP_`, and separators are normalized. Unknown names are rejected.
Deep ROM checks can reject an unknown warp, NPC, or out-of-bounds coordinate.

A `collect` objective with `target.species` is an NPC gift objective. The game indexes non-trade
`givemon` commands from the ROM (including the gift level and prerequisite flags), routes to the
unique remaining gifting NPC, and uses `dex_owned` as the derived completion probe. A received gift
is an exhausted source; an unmet prerequisite remains a route gate and is reported as such. ROM
source facts also expose item, money, and coin gates (or `unknown` when a gate cannot be decoded).

## 4. Probes: facts, not claims

The tracker evaluates every probe against the current usable RAM snapshot. An unusable frame does not
prove success, and success is never taken from model text.

| Kind | Fields | Fact tested |
|---|---|---|
| `map_arrival` | `map` | current frame is in that map |
| `flag` | `flag` 1..0x8FF, optional `expected` | saved event flag equals `expected` |
| `var` | `var` 0x4000..0x40FF, `value` | event variable equals value |
| `badge` | `badge` 1..8 | corresponding badge flag is set |
| `trainer_defeated` | `trainer_id` | trainer flag `0x500 + trainer_id` is set |
| `party_healthy` | optional `min_fraction` | every party member has positive HP and the requested fraction (HP only) |
| `party_level` | `min_level` 1..100 | every party member reaches that level |
| `mon_level` | exactly one of `personality` (0..0xFFFFFFFF) or `slot` (0..5), plus `min_level` 1..100 | the identified live party member reaches that level; an absent member is unknown/not satisfied |
| `party_species` | `species` 1..151 | party contains that species |
| `party_knows_move` | `move_id` 1..354 | a party member knows that move |
| `game_clear` | none | game-clear flag is set |
| `item` | `item` 1..374, optional `min` or `min_quantity` | bag quantity, summed across pockets |
| `money_at_least` | `amount` 0..999999 | player money reaches amount |
| `dex_owned` | `species` 1..151 | species is marked caught |
| `dex_count` | `min` 0..151 | caught count reaches min |

For example, this delegates training a specific live party member without adding a strategy:

```json
{"kind":"mon_level","personality":305419896,"min_level":25}
```

The personality and level are read from party RAM. Use `slot` instead when the request is about a
party position; the probe remains false when that slot or personality is absent.

`ObjectiveTracker.update(snapshot)` emits `progress.goal_changed` when the tracked revision changes
and `progress.milestone_completed` once all probes are true. `ObjectiveSession.observe()` evaluates
the same probes and commits completion only at a fresh usable safe boundary.

## 5. Validation and writing

`validate()` and `ObjectiveStore.propose()` return typed reasons. Common codes are `schema_invalid`,
`unknown_map`, `missing_target`, `unreachable_target_type`, `unknown_warp`, `unknown_npc`,
`out_of_bounds`, `bad_probe`, `expired`, and `stale_revision`. Removed Phase 3 fields produce
`schema_invalid`, with the model error naming the removed field.

In process, use compare-and-set:

```python
store = ObjectiveStore(path, directory=RomMapDirectory(rom_bytes, profile))
result = store.propose(objective_dict, expected_revision=17)
if not result.accepted:
    plan_again(result.as_dict()["reasons"])
```

For a separate writer, write a temporary file in the same directory, `fsync`, and atomically rename it;
`pokegent.goals.atomic_write_json()` supplies that operation. Never write a partial final file.

## 6. Examples

Reach the league lobby (the empty probe list derives map arrival):

```json
{
  "objective_id": "reach_league_lobby",
  "revision": 1,
  "kind": "reach",
  "target": {"map": "IndigoPlateau_PokemonCenter_1F"},
  "rationale": "Eight badges. Reach the league lobby."
}
```

Heal in the lobby (the empty probe list derives party health):

```json
{
  "objective_id": "heal_before_lorelei",
  "revision": 2,
  "kind": "heal",
  "target": {"map": "IndigoPlateau_PokemonCenter_1F"},
  "rationale": "The party is hurt; heal before the league."
}
```

Enter Lorelei's room through the lobby warp; the destination must be explicit:

```json
{
  "objective_id": "enter_lorelei",
  "revision": 3,
  "kind": "reach",
  "target": {"map": "IndigoPlateau_PokemonCenter_1F", "warp": 1},
  "success_probes": [{"kind": "map_arrival", "map": "PokemonLeague_LoreleisRoom"}],
  "rationale": "Enter Lorelei's room."
}
```

Fight Lorelei; the next room is the completion fact:

```json
{
  "objective_id": "beat_lorelei",
  "revision": 4,
  "kind": "fight",
  "target": {"map": "PokemonLeague_LoreleisRoom", "npc": 1},
  "success_probes": [{"kind": "map_arrival", "map": "PokemonLeague_BrunosRoom"}],
  "rationale": "Defeat Lorelei and reach Bruno's room."
}
```

Train until every party member reaches level 30 and the party remains at least half healthy:

```json
{
  "objective_id": "train_route21",
  "revision": 10,
  "kind": "train",
  "target": {"map": "Route21_North"},
  "success_probes": [
    {"kind": "party_level", "min_level": 30},
    {"kind": "party_healthy", "min_fraction": 0.5}
  ],
  "rationale": "Train in the Route 21 area."
}
```

Prove the HM02 gift with its story flag and item:

```json
{
  "objective_id": "get_hm02_fly",
  "revision": 11,
  "kind": "talk",
  "target": {"map": "Route16_House", "npc": 1},
  "success_probes": [
    {"kind": "flag", "flag": 568},
    {"kind": "item", "item": 340}
  ],
  "rationale": "Obtain HM02 from the person in the Route 16 house."
}
```

Hold position with no target:

```json
{"objective_id": "hold_position", "revision": 12, "kind": "hold", "rationale": "Wait."}
```

## 7. Scripted plan queue

`ScriptedObjectiveSource(store, plan)` validates a list of objectives up front and proposes them in
order. It writes through the store, uses `set_by: script` by default, and advances only after the
current objective's probes are true and the session observes a fresh-RAM safe boundary. A foreign
A foreign agent/operator objective is not replaced. Review and abort fields are not part of the queue contract.

The file form is `objective-plan/1`; a bare list is also accepted. `revision` may be omitted and is
assigned by the queue; entries must have unique ids and there can be at most 64.

```json
{
  "schema_version": "objective-plan/1",
  "objectives": [
    {"objective_id": "enter_league", "kind": "reach", "target": {"map": "IndigoPlateau_PokemonCenter_1F"}, "rationale": "Reach the lobby."},
    {"objective_id": "heal_before_lorelei", "kind": "heal", "rationale": "Heal the party."},
    {"objective_id": "enter_lorelei", "kind": "reach", "target": {"map": "IndigoPlateau_PokemonCenter_1F", "warp": 1}, "success_probes": [{"kind": "map_arrival", "map": "PokemonLeague_LoreleisRoom"}], "rationale": "Enter Lorelei's room."}
  ]
}
```

The queue stops if an entry is rejected and records the typed reasons. After the last entry completes,
the runner applies `on_objective_done`.

## 8. Runner lifecycle and configuration

At the start of each iteration the session refreshes the store before perception. The current objective
id, summary, revision, and `forbid` view are supplied to the frame and field builder. After perception,
the tracker evaluates probes on the current snapshot. A completed objective is recorded only at the
first fresh usable idle scene with known non-battle controls (`field.free`-style safe boundary).

`run.on_objective_done` controls the completed-objective policy: `hold` (default) keeps the run alive without input
and continues polling; `pause` waits at the safe boundary with `WAITING_OBJECTIVE`; `continue`
atomically clears the completed objective and keeps playing in the same run, goal-less or under a
newer/queued objective. The transition checks both objective ID and revision under the store's
writer lock; it never removes a successor. It records completion once and a durable
`completion_cleared` history entry before removing the file and resetting the in-memory goal.
Replaying the old completed revision cannot reactivate it; the revision floor survives clearing.
The next decision is rebuilt from fresh perception. New revisions, including updates during a
pending agent or hybrid choice, are adopted without stopping, signalling or restarting the runner.
The old pending frame ID is rejected after refresh, and executor legality checks still apply.

Fresh completion authorizes clearing agent and operator objectives in this specific transition;
manual clear requests still require the matching revision and operator authority for an operator
objective. File writers must use `ObjectiveStore.propose()` (or serialize on `<file>.lock` and
use atomic replacement) to participate in the compare-and-set protocol. Keep the history with the
objective file across restarts. `hold` remains the default; `pause` is unchanged.

`run.ignore_objective` / CLI `--ignore-objective` lets a
satisfied objective continue without the normal completion wait. CLI objective inputs are
`--objective`, `--objective-plan`, and `--on-objective-done`.

The supported configuration names relevant here are `run.on_objective_done` and
`run.ignore_objective`; the removed review/stuck keys are listed above and are rejected.
