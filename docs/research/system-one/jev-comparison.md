# R03 — Jev and System One research

**Status:** source-backed research proposal; no runtime implementation or benchmark results generated. **Date:** 2026-11-24. Pokegent baseline: [`c73774285e5e164d01629ca14a5e8e551e03493f`](https://github.com/aimd-labs/pokegent/commit/c73774285e5e164d01629ca14a5e8e551e03493f). This proposal builds on R01/R02 and existing issues [#132](https://github.com/aimd-labs/pokegent/issues/132), [#133](https://github.com/aimd-labs/pokegent/issues/133), [#134](https://github.com/aimd-labs/pokegent/issues/134), [#135](https://github.com/aimd-labs/pokegent/issues/135), [#138](https://github.com/aimd-labs/pokegent/issues/138), [#139](https://github.com/aimd-labs/pokegent/issues/139).

## Decision

Retain Pokegent's typed decisions, fail-closed game adapter, single-writer executor, exact frame identity and verified postconditions. The strongest external evidence supports semantic actions, compact decision-specific facts, durable objective ownership, bounded deterministic execution and independently measured decision authority. It does not support giving a classifier control over unknown scene legality, adopting an arbitrary confidence threshold, or requiring a strategic external agent in every mode.

## Sources and licensing

| Reference | Pinned commit | License reviewed | Disposition |
|---|---|---|---|
| [christianmat/jev-pokemon](https://github.com/christianmat/jev-pokemon/tree/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd) | `20cdcb846ff995383fe3da1d48ad5eac6d8d1abd` | [GPL-2.0 root; README declares GPL-2.0-or-later](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/LICENSE) | REFERENCE |
| [anxkhn/JevPlaysPokemon](https://github.com/anxkhn/JevPlaysPokemon/tree/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e) | `aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e` | [GPL-3.0](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/LICENSE) | REFERENCE |
| [daniel4x/JevEmon](https://github.com/daniel4x/JevEmon/tree/572454c69bf791f766ece736ffa6d6c954c6a075) | `572454c69bf791f766ece736ffa6d6c954c6a075` | [GPL-3.0](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/LICENSE) | REFERENCE |
| [2389-research/jev-plays-pokemon](https://github.com/2389-research/jev-plays-pokemon/tree/75193d9883bea71be66647bcb6cefcf441c932e4) | `75193d9883bea71be66647bcb6cefcf441c932e4` | [MIT](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/LICENSE) | REFERENCE |

No source code, datasets, prompts, ROM fixtures or assets copied; no new dependency. MIT reference may be considered for later adoption after per-file provenance and notices review. GPL references remain behavioral research only; do not assume decompilation or game assets are permissively licensed. NousResearch and Clad3815 broader harness divergence belongs to R04; battle math and simulators to R06. Existing historical VON contract evidence remains pinned in [source-lock.md](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/source-lock.md) and [provider integration](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/provider-integration.md); it is not a statement of current TypeSafe, VON or other live API support. No network model call was made.

## 1. Evidence is not interchangeable

| Project | Reported result | Important boundary |
|---|---|---|
| christianmat | Red completed in 37h40m, 16,150 decisions, ~39.2M input tokens, ~$1.65 model cost, median ~0.4s, 16 wipes (14 in League), 15 League attempts. | Single author-reported campaign; authored milestones, RAM facts, harness rules and recovery behavior; not a controlled FireRed comparison. [README](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/README.md) |
| anxkhn | Showdown seed 1,2,3,4: 5 separate wins, 55 turns, 63 API decisions, 212,177 input tokens. Actual FireRed: isolated Lorelei, Bruno and Agatha wins; subsequent consecutive run Lorelei win then Bruno loss. | Fresh healed party for each Showdown match, simple opposing heuristic, custom teams, privileged state. Native Rev 1 runs use prepared states and RAM-written party. Level-100 baseline progression is not Jev evidence. **No complete native level-40 league win.** [RESULTS](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/RESULTS.md), [architecture](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/ARCHITECTURE.md) |
| daniel4x | House → Pallet Town → Route 1 → Viridian City with battle interruption handling reported. | Prepared checkpoint and configured party written to RAM; not a full game, not cold boot. [README](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/README.md), [architecture](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/ARCHITECTURE.md) |
| 2389 | Source shows hierarchical planner, directives, flow router, battle objectives and tests/eval scripts. | README requires manually played intro; no verified end-to-end result established here. [README](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/README.md) |

Do not transfer reported prices/latency to a different model, provider, game, observation policy or benchmark. No independent replay or test of these claims. Distinguish simulator victory, isolated native scenario, prepared checkpoint segment, full uninterrupted league, and cold-to-credits campaign in all releases.

## 2. christianmat: strategic intent and failure memory

[Agent progress tracking](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/agent.ts#L50-L115) uses milestone, visited maps, event flags, party size, objective-conditioned experience and puzzle progress, not merely a successful button press. [Intent selection](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/overworld.ts#L1085-L1202) offers progress/heal/train/catch/shop/explore/team; the focus persists across small changes, expires after bounded decisions and re-evaluates on material state changes. The idea is valuable: ask strategic questions when circumstances actually change, not each tile. Author-reported 15 League attempts motivate measuring repeated failure and team readiness, not a claim that one change would eliminate those attempts.

Details that must **not** be inherited:
- [Loop exploration](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/jev/client.ts#L95-L135) uses randomized sampling after a stuck threshold. Pokegent's production contract prohibits randomness in decisions; if experimental sampling is ever studied, isolate it in offline/explicit reproducible research, never default execution.
- The checkpoint rollback is opt-in (default Infinity in [agent.ts](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/agent.ts#L10-L12)), despite broad README language. It can discard real progress and is not a recovery default for Pokegent.
- Loss-gate code exists but `LOSS_RETRY_MS = 0` at the pin. Do not describe it as an active reliable protection. Some intent gates use wall-clock cooldowns and heuristic progress; Pokegent needs typed RAM evidence and bounded objective revisions.
- [Client](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/jev/client.ts#L64-L135) hashes state/questions for a bounded cache, can tick idle while waiting and retries; [gateway](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/jev/gateway.ts#L1-L37) also retries. Nested retries and cached low-confidence/old state choices are not automatically safe or cost-optimal. Keep total deadline and fresh frame authority. Use cache in replay/evaluation first, not automatic production reuse.
- Authored [milestones](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/knowledge/milestones.ts) mix RAM and remembered visits; only independent current-state proofs can complete Pokegent objectives.

## 3. anxkhn: dual benchmark backends and stable identity

[Showdown loop](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/battle.js#L17-L119) derives options from simulator requests and submits only a valid action; [tests](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/battle.test.js) check disabled/trapped/forced choices, category rules, invalid IDs, probability sums and budget failures. [Jev adapter](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/jev.js#L25-L78) validates full per-question distributions, confidence and choice. [Native runner](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/emulator.py#L69-L146) resolves Pokémon identity after menu stabilization and [JevStrategy](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/emulator.py#L324-L439) verifies the actual active identity on the next decision. Reuse the idea of exact candidate→selection→outcome identity with the existing Pokegent executor and party fingerprint; don't copy implementation or assume personality alone uniquely identifies every stored Pokémon.

A shared **semantic benchmark contract**, not shared raw save or fake equality between engines:
- Same legal choice/fact semantics where supported; versioned Gen-3 mechanics and explicit observable vs privileged policy.
- Keep simulator policies, starting team, seed, resource restoration, and native preparation method in the manifest. Compare within each backend; never combine wins across engines as campaign success.
- Full distributions matter for calibration; parsers should distinguish valid choice with missing uncertainty data from a valid fully calibrated response. Provider response shape checks are not proof of tactical quality.
- Separate legal choice, verified execution, and effective battle outcome. A legally executed immune move may be a tactical failure. A correct tactical choice may be followed by a miss or adverse RNG. Set-valued labels allow multiple reasonable moves; do not use winner alone as per-turn gold.
- Use independent ROM-free cases and private native scenarios. R06 owns authoritative formula/differential vectors and simulator integration decisions.

## 4. daniel4x: semantic navigation and interruption boundaries

[Journey.valid_destinations/decide](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/journey.py#L122-L169) validates reachable destinations, skips a model call for the only candidate and supplies visit counts. [Travel/take](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/journey.py#L245-L310) distinguishes reached vs blocked vs battle interruption, releases held input when a battle begins, and counts a destination as visited only after arrival. These are directly useful for #180/#133/#132. Do not copy its blocked-set policy or assume a map change alone proves the correct destination: verify exact destination/map, path ownership and current frame. Use per-state/capability scoped failure history and bounded replan; never permanently suppress all equivalent exits after one transient failure. Its approach uses prepared Rev1 RAM-write fixtures and generator controls, not Pokegent's production safety invariants. No automatic unverified A-confirmation on a transition. Keep current world graph and #106 decisions.

## 5. 2389-research: hierarchy with one active objective

[Executive loop](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/reason_loop.py#L211-L320) has RAM-owned battle dispatch, a current directive, deterministic target navigation and residual Jev choices. [L1 pipeline](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/l1_pipeline.py) validates completion criteria, repairs once and retains prior plan on invalid/no-op output. [Needs arbiter](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/needs_arbiter.py) illustrates hysteresis and bounded training priority. [Battle L2](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/games/pokemon_red/battle_l2.py#L76-L120) caches battle intent with safety reflexes; its concrete thresholds and slot/HP assumptions are not universally correct. [Routing tests](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_routing.py) differentiate shortest vs avoiding or seeking grass; [confidence tests](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_reason_loop_confidence.py) check reflection scheduling, not actual model reliability.

Use these ideas to improve Pokegent's existing objective session/CAS, not add another competing quest store. A high-level policy decides desired objective/route preference (progress, heal, capture, train, explore, etc.); facts/legality remain adapter-owned. A bounded objective proposal must contain validated target and RAM success conditions, and must preserve a newer operator objective. A battle sub-intent is a fact/policy context, not authority to mask a safe alternative arbitrarily.

Do **not** adopt the classifier-based input authority in [actual _route_flow](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/reason_loop.py#L1472-L1512). It can select menu/dialogue/navigation from model confidence and fall back to dialogue based on raw text. [Design](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/docs/superpowers/specs/2026-09-21-jev-flow-router-design.md) and [grader](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_flow_gate_grader.py) do not prove legality. Pokegent may use visual/text classification as diagnostic or agent hint on unknown scene; a model must never promote unknown to legal or authorize an A press. Known menu/battle ownership and action verification remain deterministic.

## 6. Synthesis

| Lesson | Pokegent work | Capability IDs |
|---|---|---|
| Semantic choices, no button-level model planning | [#132](https://github.com/aimd-labs/pokegent/issues/132) pre-call proof and deterministic route provenance | runtime.execution, world.navigation |
| Persistent focus and one revisioned objective | objective proposals and completion checks; [#133](https://github.com/aimd-labs/pokegent/issues/133), [#135](https://github.com/aimd-labs/pokegent/issues/135) | runtime.objectives, strategy.resources, strategy.team_optimization |
| Avoid repeated failure without erasing progress | [#133](https://github.com/aimd-labs/pokegent/issues/133), [#137](https://github.com/aimd-labs/pokegent/issues/137), [#180](https://github.com/aimd-labs/pokegent/issues/180) scoped recovery and first bad transition | runtime.hybrid, runtime.telemetry |
| Stable Pokémon identity across menu | existing battle/PC executor assertions; [#165](https://github.com/aimd-labs/pokegent/issues/165), [#137](https://github.com/aimd-labs/pokegent/issues/137) | battle.switch, pokemon.identity |
| Evaluation at distinct layers | [#134](https://github.com/aimd-labs/pokegent/issues/134), [#135](https://github.com/aimd-labs/pokegent/issues/135), [#138](https://github.com/aimd-labs/pokegent/issues/138), R06/R09 | runtime.system_one, runtime.agent, runtime.hybrid |
| Provider-independent contracts | [#134](https://github.com/aimd-labs/pokegent/issues/134), [#139](https://github.com/aimd-labs/pokegent/issues/139) strict identity, safe uncertainty semantics | runtime.game_plugin, runtime.system_one |

See [decision architecture proposal](../../architecture/decision-routing.md) and [proposed scenario catalog](system-one-evaluation.json). Source evidence index (paths reviewed at the stated pins, some entry points only):

### christianmat/jev-pokemon

- [README.md](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/README.md)
- [LICENSE](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/LICENSE)
- [src/agent/agent.ts#L50-L140](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/agent.ts#L50-L140)
- [src/agent/context.ts](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/context.ts)
- [src/agent/overworld.ts#L1085-L1202](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/overworld.ts#L1085-L1202)
- [src/agent/battle.ts](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/agent/battle.ts)
- [src/jev/client.ts#L64-L135](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/jev/client.ts#L64-L135)
- [src/jev/gateway.ts#L1-L37](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/jev/gateway.ts#L1-L37)
- [src/knowledge/milestones.ts#L71-L87](https://github.com/christianmat/jev-pokemon/blob/20cdcb846ff995383fe3da1d48ad5eac6d8d1abd/src/knowledge/milestones.ts#L71-L87)

### anxkhn/JevPlaysPokemon

- [README.md](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/README.md)
- [LICENSE](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/LICENSE)
- [ARCHITECTURE.md](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/ARCHITECTURE.md)
- [RESULTS.md](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/RESULTS.md)
- [RESEARCH.md](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/RESEARCH.md)
- [jev.js#L25-L78](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/jev.js#L25-L78)
- [battle.js#L17-L119](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/battle.js#L17-L119)
- [battle.test.js](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/battle.test.js)
- [emulator.py#L69-L146](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/emulator.py#L69-L146)
- [emulator.py#L324-L439](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/emulator.py#L324-L439)
- [emulator_checks.py](https://github.com/anxkhn/JevPlaysPokemon/blob/aa450c686da75b9bdaa4aa5972ee4d02cfca9d8e/emulator_checks.py)

### daniel4x/JevEmon

- [README.md](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/README.md)
- [LICENSE](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/LICENSE)
- [ARCHITECTURE.md](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/ARCHITECTURE.md)
- [jevemon/decisions.py#L9-L64](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/decisions.py#L9-L64)
- [jevemon/journey.py#L122-L196](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/journey.py#L122-L196)
- [jevemon/journey.py#L245-L310](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/journey.py#L245-L310)
- [jevemon/runner.py](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/jevemon/runner.py)
- [tests/test_journey.py](https://github.com/daniel4x/JevEmon/blob/572454c69bf791f766ece736ffa6d6c954c6a075/tests/test_journey.py)

### 2389-research/jev-plays-pokemon

- [README.md](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/README.md)
- [LICENSE](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/LICENSE)
- [docs/superpowers/specs/2026-09-21-jev-flow-router-design.md](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/docs/superpowers/specs/2026-09-21-jev-flow-router-design.md)
- [src/pokemon_agent/agent/reason_loop.py#L211-L367](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/reason_loop.py#L211-L367)
- [src/pokemon_agent/agent/reason_loop.py#L1472-L1512](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/reason_loop.py#L1472-L1512)
- [src/pokemon_agent/agent/l1_pipeline.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/l1_pipeline.py)
- [src/pokemon_agent/agent/needs_arbiter.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/needs_arbiter.py)
- [src/pokemon_agent/agent/typesafe_reasoner.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/typesafe_reasoner.py)
- [src/pokemon_agent/agent/routing.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/agent/routing.py)
- [src/pokemon_agent/games/pokemon_red/battle_l2.py#L76-L120](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/games/pokemon_red/battle_l2.py#L76-L120)
- [src/pokemon_agent/logging/run_recorder.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/src/pokemon_agent/logging/run_recorder.py)
- [scripts/eval_criteria.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/scripts/eval_criteria.py)
- [tests/unit/test_routing.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_routing.py)
- [tests/unit/test_reason_loop_confidence.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_reason_loop_confidence.py)
- [tests/unit/test_flow_gate_grader.py](https://github.com/2389-research/jev-plays-pokemon/blob/75193d9883bea71be66647bcb6cefcf441c932e4/tests/unit/test_flow_gate_grader.py)

## Limitations

Source selection and test inspection, not exhaustive audits. No reference programs, live providers, Pokegent runtime, ROMs or benchmarks executed. No compatibility or capability status upgraded. No changes to existing issue labels, status, assignments, live runtime or release. The architecture and numeric evaluation gates below are proposals; existing contracts remain binding until a reviewed implementation/ADR changes them.
