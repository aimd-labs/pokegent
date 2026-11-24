# R06 — Battle intelligence, team planning and data conformance

Proposed research, 2026-11-24. Runtime baseline c73774285e5e164d01629ca14a5e8e551e03493f. No executable code, imported data or dependency; selected source review and test design only. Related [#134](https://github.com/aimd-labs/pokegent/issues/134), [#139](https://github.com/aimd-labs/pokegent/issues/139), R02/R03/R05/R09.

## Evidence and license

| Source | Pin | Inspection and intended use |
|---|---|---|
| Showdown | [e7f50cd7bcb36348c631534a97ae8323b8189541](https://github.com/smogon/pokemon-showdown/tree/e7f50cd7bcb36348c631534a97ae8323b8189541) | [MIT](https://github.com/smogon/pokemon-showdown/blob/e7f50cd7bcb36348c631534a97ae8323b8189541/LICENSE); [Gen3 modifiers](https://github.com/smogon/pokemon-showdown/blob/e7f50cd7bcb36348c631534a97ae8323b8189541/data/mods/gen3/scripts.ts), [choice requests](https://github.com/smogon/pokemon-showdown/blob/e7f50cd7bcb36348c631534a97ae8323b8189541/sim/side.ts), [team schema](https://github.com/smogon/pokemon-showdown/blob/e7f50cd7bcb36348c631534a97ae8323b8189541/sim/teams.ts); reference / optional isolated simulator |
| damage-calc | [abdce0c64597284c8c463b7f71a38dc4dc55cd10](https://github.com/smogon/damage-calc/tree/abdce0c64597284c8c463b7f71a38dc4dc55cd10) | [MIT](https://github.com/smogon/damage-calc/blob/abdce0c64597284c8c463b7f71a38dc4dc55cd10/LICENSE); [generation dispatch](https://github.com/smogon/damage-calc/blob/abdce0c64597284c8c463b7f71a38dc4dc55cd10/calc/src/calc.ts), [ADV mechanics](https://github.com/smogon/damage-calc/blob/abdce0c64597284c8c463b7f71a38dc4dc55cd10/calc/src/mechanics/gen3.ts); differential oracle candidate |
| PKHeX | [a5311f9f55a84376fd2e2499b25d9fb9640d9f2d](https://github.com/kwsch/PKHeX/tree/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d) | [GPL-3.0 root](https://github.com/kwsch/PKHeX/blob/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d/LICENSE); [PK3](https://github.com/kwsch/PKHeX/blob/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d/PKHeX.Core/PKM/PK3.cs), [crypto](https://github.com/kwsch/PKHeX/blob/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d/PKHeX.Core/PKM/Util/PokeCrypto.cs), [FRLG save](https://github.com/kwsch/PKHeX/blob/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d/PKHeX.Core/Saves/SAV3FRLG.cs), [legality](https://github.com/kwsch/PKHeX/blob/a5311f9f55a84376fd2e2499b25d9fb9640d9f2d/PKHeX.Core/Legality/LegalityAnalysis.cs); REFERENCE only |
| pkmn/ps | [2a0a161c4ca3cae4f0c7d02e31bcfcb542263601](https://github.com/pkmn/ps/tree/2a0a161c4ca3cae4f0c7d02e31bcfcb542263601) | MIT metadata, [README](https://github.com/pkmn/ps/blob/2a0a161c4ca3cae4f0c7d02e31bcfcb542263601/README.md); modular simulator/dex/data/protocol candidate, implementation not reviewed |
| poke-env | [bab3673909666f68814a239dfa2a957c1f5c07aa](https://github.com/hsahovic/poke-env/tree/bab3673909666f68814a239dfa2a957c1f5c07aa) | MIT metadata, [README](https://github.com/hsahovic/poke-env/blob/bab3673909666f68814a239dfa2a957c1f5c07aa/README.md), [doubles request parsing](https://github.com/hsahovic/poke-env/blob/bab3673909666f68814a239dfa2a957c1f5c07aa/src/poke_env/battle/double_battle.py); Python benchmark client, not runtime dependency |

All five trees returned without truncation. MIT code grants do not establish rights to game datasets. Review specific code, notices, dependency/data rights before adoption. Keep GPL PKHeX separate as a technical format/behavior reference; no code, generated tables, saves or fixture copying. No online battles, native tests or executable simulator runs.

## Current implementation and gap

[Damage estimate](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/ruleset/damage.py) already covers uncertainty ranges for opponent stats and equipment; [views](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/mechanics/views.py) distinguish unknown from none; [catch](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/ruleset/catch.py) has probability/ball/odds functions; [exchange](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/mechanics/exchange.py) models bounded turn exchanges; [team planning](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/mechanics/team_planning.py) uses existing estimates and speed tiers. Keep these Python mechanics and exact executor; do not rebuild them in JS or replace with a remote calculator.

Showdown Gen3 code explicitly derives physical/special from type, has Gen3 replacement scheduling, ordered modifiers, spread modifier and weather/crit logic. Damage-calc dispatches by generation and handles special cases before the ADV formula. They are useful independent comparators, but not cartridge ground truth: badge boosts, trainer AI, bag actions, Safari, story restrictions and information visibility differ. Compare like-for-like exact inputs and declare exclusions; use pret/local ROM evidence to resolve disagreements, not majority vote. PKHeX demonstrates party/stored format, checksum-dependent decryption, version-specific inventory/save and encounter legality, but editing a save or passing a legality analyzer is not gameplay proof. Existing decoder already checks checksums; extend conformance not a second decoder.

## Proposed contracts

1. Mechanics input = title/revision/ruleset ID, battler and target identity, stats, stages, moves/PP, field, side, status, equipment, visibility, intent, unknowns. Keep adapter-specific fields local. Result distinguishes deterministic rule, conditional range, exact distribution, approximation and unknown. Never hide uncertainty or promote tactical value into legality.
2. Legal actions are current scene facts; rank/score is advisory. Model sees damage/KO, accuracy/priority, effect, switch/resource consequences where proved, with explicit unknowns. Doubles requires battler and target identities, forced replacement and menu slot revalidation. No fixed best-move policy.
3. Team optimization is ordinary core: candidate party/PC identities, obtainable moves/items/evolutions, matchup and coverage, EXP/EV targets, resource/time/prerequisite costs. Produce alternatives and tradeoffs, not a mandatory competitive-team strategy; no version-exclusive or event-only acquisition without evidence. Breeding and training remain core.
4. Optional isolated local simulator adapter under benchmark tooling: pinned package/version, explicit format and ruleset, synthetic team, deterministic seed, opponent policy, observed vs privileged state, no network or public credentials by default. Use same semantic choice labels where equivalent; never route simulator commands into emulator. Python core has no mandatory Node/Showdown dependency.
5. Differential harness (R09/R06 implementation): per-vector independently generated inputs, expected rule/result and source revision, compare exact integer distributions if known; separate inapplicable and unknown, classify discrepancy with cartridge proof before fix. Use ROM-free synthetic vectors in public CI and private native scenario manifests. Report per-rule coverage and failure, not blanket accuracy. Existing R03 scenario labels may be set-valued; model quality vs mechanics correctness separate.

## Proposed acceptance cases (not executed)

| ID | Surface | Scenario | Acceptance |
|---|---|---|---|
| B01 | physical/special split | Gen3 type category; distinguish status, dynamic type and Hidden Power | known input damage/category vs independent source |
| B02 | integer ordering | rounding at each documented step, min/max rolls and critical modifier | compare full ordered rolls for exact inputs; no blanket tolerance |
| B03 | type and ability | immunity, Levitate, Wonder Guard, Thick Fat, absorption | zero/effect and legal choice separately |
| B04 | field | weather, Cloud Nine, screens, doubles spread, badge boosts | cartridge-specific cases vs simulator subset |
| B05 | unknown inputs | enemy HP bar, unknown IV/EV/nature/ability/item | honest bounds, missingness, no invented probability |
| B06 | variable/fixed effects | multi-hit, fixed damage, recoil, self-KO, secondary effects | typed effect and conditional outcomes |
| B07 | speed and priority | speed tie, priority, paralysis, forced switch | turn order and exact target identity |
| B08 | capture | ball, status, catch rate, Safari, no ball, trainer | ROM rule oracle; distinguish no capture from executor failure |
| B09 | doubles | two active slots, partner target, forced replacement | legal target enumeration and fresh owner |
| B10 | switch | party cursor reorder; identity after confirmation | no wrong-mon input; actual active identity |
| B11 | PK3 decoder | all PID substructure permutations, checksum, egg, invalid length | independently authored synthetic vectors; refuse invalid |
| B12 | storage/save | party vs boxed structure, slot bounds, version-specific save blocks | read-only conformance; no save editing |
| B13 | team optimization | learnset/legal availability, PC candidates, level, EV/DV, resource and matchup | recommend feasible alternatives, explicit constraints |
| B14 | campaign relevance | continuous league vs independent simulator fights | native outcome proof with resources carried across trainers |

Order: characterize existing damage and catch rules first; add minimal differential fixtures; compare isolated simulator on overlapping Gen3 facts; expand team/PC/doubles/ordinary mechanics under existing capability IDs; private native proof before status promotion. R05 determines exact title semantics, R07 knowledge provenance, R09 evaluation. No implementation claims or issue mutations.
