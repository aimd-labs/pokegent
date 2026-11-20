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

`instructions`, `abort_if` and `runner.review_events` remain removed and are rejected.

### Opt-in stuck review

`next_review_on: ["stuck"]` explicitly requests bounded advisory review of an unmet
objective. The default is empty; other events and duplicate entries are rejected.
`run.objective_stuck_after` is an integer >=1 (default120) of usable updates without
new progress. Progress means a newly visited 4x4 map cell, changed flag count or an
increase in satisfied probes. Cell memory is capped at4096 and clears at that cap;
visits can count as new again after the cap. Hold/completed/absent goals are exempt.
Progress re-arms one request per episode; goal id/revision changes reset tracking.

`run.on_objective_review` accepts `continue` (default), `pause` or `stop`. Continue
keeps the last valid objective; it does not clear, complete, replace or advance it.
Review never overrides hard no-progress, hybrid recovery, operator ownership or
fresh-RAM legality. A newer accepted objective clears pending review; queued facts
retain their originating id/revision, and a stale response cannot stop a successor.
The bounded queue holds64 requests; request counters saturate at2^31-1. Empty
subscriptions are omitted from serialization to preserve older history equality;
explicit stuck subscriptions persist across restart. No abort/recovery policy,
route assist or story successor is supplied by this feature.

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
| `var_at_least` | same fields and bounds as `var` | readable event variable is at least value |
| `any_of` | `children`: 1..8 leaf probes | at least one child is true; nested groups are rejected |
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

The outer `success_probes` list remains a conjunction. For a consumable item with persistent
proof, use `{"kind":"any_of","children":[{"kind":"item","item":17},
{"kind":"flag","flag":7}]}`. Unknown or unusable RAM never proves a leaf. A readable sparse
variable table supplies implicit zero; an absent table is unknown. Script effects may mark an
interaction as serving either new probe, but only fresh usable RAM can prove completion.
RAM-only hold groups wait exactly like existing flag/var holds; groups containing other leaves
remain actionable. Existing probe and objective JSON gains no fields or defaults.

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

History records must be written in full and fsynced before a completion clear can remove the
objective. A failed write preserves the file; the next history append discards any incomplete
last line under the writer lock before retrying.

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

## 9. Opt-in successor fallback

Existing plan queues advance to their next entry at a fresh safe completion boundary. To continue
with a ROM-backed dex objective after the queue or a standalone objective completes, configure:

```yaml
run:
  on_objective_done: continue
objectives:
  fallback: dex  # "off" by default; quote "off" in YAML
```

The run uses its existing objective file (default `<run dir>/objective.json`); in-process callers
must supply a file/store to `build_runner`. Fallback proposals use the same validator, writer lock,
compare-and-set, history and revision floor. A newer writer wins. Active agent/operator objectives
and pending/rejected queues take precedence. Expiry does not prove completion or advance a queue.
`hold` and `pause` retain their completed-objective behavior; fallback can also fill an initially
empty store from a fresh usable safe frame. With fallback off, empty files remain goal-less.

Fallback loads encounters only from the existing private ROM cache (supported ROM and local map
data must already be supplied outside the checkout). It ranks unowned species with land encounters,
using the fork's encounter-probability/current-map cost. It emits a `train` objective with a
`dex_owned` RAM probe, not a story milestone or a guarantee that the encounter is reachable.
No species-name or town table is shipped. Surf/fishing encounters are excluded in this slice.

Known full-HP non-egg party RAM, Pokedex RAM and at least ten capture balls (item IDs 2, 3 or 4)
are required. Capture eligibility retains the fork's HP-only check; it does not prove full PP or
absence of status. The stronger public heal completion probe (HP, status and ROM-backed PP) is unchanged.
The fork's heal/restock prerequisite intent is retained, but automatic destinations are not inferred:
unresolved heal/restock, unknown facts, no remaining supported candidates, expired/rejected input,
or rejected fallback proposals emit `runtime.paused` (`fallback_unavailable`) and SAFE_PAUSE (exit 1)
at the safe boundary. Supply an explicit valid objective and resume. Missing/invalid private
encounter data is a setup error (exit 4). Data generation, automatic shop/healing routes and complete
story progression are outside this slice. Synthetic tests establish frame handoff, not a real run.

## 10. Opt-in authored story source (synthetic engine slice)

```yaml
run:
  on_objective_done: continue
objectives:
  fallback: story
```

The configuration file itself must say `continue`. AppConfig rejects missing/default
`hold` or `pause` before CLI overrides apply, so CLI `continue` cannot rescue that file.
A later CLI `hold`/`pause` override is also rejected. Off and Dex retain their existing
behavior. Story needs the same objective store; it never replaces an active manual or
operator goal, bypasses a pending/rejected queue, or treats an expired file as empty.

Supply `progression.json` in `POKEGENT_LOCAL_DATA_DIR`, outside the checkout (including
symlink targets). The independent fixed loader accepts at most 64 KiB and does not
add an eighth local-data/setup schema. All records must validate before use:

- `schema_version`: 1.
- `provenance`: `rom_sha256`, `source_sha256`, `map_index_sha256` (lowercase 64-digit
  hex), `source_pin` and `generator` (1..128 characters).
- `policy`: `order` (every node id exactly once) and nonempty `required` (unique
  subset). This is explicit authored intent, not an order extracted from ROM.
  Readiness currently means prerequisites only; other policy fields are rejected.
- `policy_sha256`: SHA256 of UTF-8 JSON of `policy`, with sorted keys and compact
  separators (`json.dumps(policy, sort_keys=True, separators=(",", ":"))`).
- `nodes`: 1..64 records with `id`, `requires` (unique node ids) and `objective` using
  the existing Objective JSON parser/validator. Node ids are 1..128 characters;
  references and policy lists have at most 64 entries. Duplicate node/objective ids,
  missing references, cycles and unknown artifact fields are rejected.

Every objective must have success probes and no expiry or review subscriptions.
This artifact version supports `flag`, `var`, `var_at_least`, `item`, and leaf
`any_of` over those kinds. Other probe kinds remain available in ordinary objectives
but are rejected in this artifact. Outer success probes retain AND semantics.
Template revisions are placeholders: Session assigns the next CAS revision and
`set_by: fallback:story`. Existing target validation still applies.

One current usable snapshot evaluates all proofs. Unknown flags/vars never prove
completion or authorize skipping a prerequisite. A readable sparse VarTable means
zero for absent entries; a usable snapshot's empty bag means no items. OR can remain
true after an item is consumed when a persistent flag proves completion. A proven
node discharges its earlier prerequisites without requiring consumed items again.
The first incomplete required milestone is selected in authored order, descending
its incomplete prerequisites in that same order to the first actionable leaf.

At the existing safe boundary, completion clears through the existing durable
history/CAS path before selecting a successor. All required nodes proven complete
produce explicit `runtime.paused` outcome/reason `story_complete`, SAFE_PAUSE (exit 1).
No empty hold or implicit Dex fallback is created. After review callbacks, Session
rechecks the empty store revision, manual successor, expiry/rejection and queue before
Runner applies that result. An external write after this final check is subject to
the same process-boundary race as existing stop decisions.

This slice has invented-data behavioral tests only. Digests declare provenance and
bind the authored policy; they do not authenticate a real ROM or establish complete
story coverage. Real generation, setup/cache identity binding, scoped temporary
facts and private phase/transition acceptance
remain separate gates. No game graph or policy is packaged with the engine.

Story mode also adds optional `story_facts` to agent/SystemOne state and the owner
briefing, including while a manual goal is active. These read-only facts use the
exact snapshot that built the offered options; they never replace or clear a goal.
`schema: story-facts/1` reports `status: available|unknown|complete`, the next
incomplete required challenge in authored order, and its prerequisite closure.
Each entry has a presentation `label` and `state: proven|unmet|unknown`. A proven
intermediate node discharges its historical prerequisites. Unknown never proves
completion; `available` means evidence is readable, not that the challenge is ready.
`policy: authored`, `provenance: declared` and `readiness: prerequisites_only`
describe the current limits explicitly.

`counts` (`proven`, `unmet`, `unknown`, `total`) cover the relevant prerequisite
closure before presentation truncation, excluding the challenge itself. Complete
story has no next challenge and an empty closure. Labels have at most 80 characters;
at most six prerequisites and one challenge fit within 1536 JSON bytes, with
`truncated: true` when detail is removed. Unsafe labels omit the whole optional
facts unit with a redacted diagnostic. Provider reduction and the 6144-byte briefing
budget may drop the whole unit; provider reductions and briefing diagnostics record
that drop. Facts do not enter action fingerprints or history context differences.
Pending goal refresh rebuilds from the same held snapshot and preserves its facts;
the briefing remains the last observed view. Off/Dex payloads stay unchanged.

The artifact's 64-node/64-KiB caps remain unchanged. Their fit for a complete real
story, including badges, key items, HMs and side gates, remains unmeasured pending
complete generation and coverage validation. Synthetic tests establish no such fit.
