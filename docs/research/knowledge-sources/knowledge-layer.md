# R07 — Local-first game knowledge and optional external providers

Status: proposed. Reviewed 2026-11-24; runtime baseline c73774285e5e164d01629ca14a5e8e551e03493f. No runtime changes, network API calls to PokéAPI, imported datasets or dependency installation.

## References
- [PokeAPI/pokeapi @ de6760be38b60a3024f1d9054330ff265f0dc619](https://github.com/PokeAPI/pokeapi/tree/de6760be38b60a3024f1d9054330ff265f0dc619): BSD-3-Clause metadata and [license text](https://github.com/PokeAPI/pokeapi/blob/de6760be38b60a3024f1d9054330ff265f0dc619/LICENSE.md); [models](https://github.com/PokeAPI/pokeapi/blob/de6760be38b60a3024f1d9054330ff265f0dc619/pokemon_v2/models.py), [serializers](https://github.com/PokeAPI/pokeapi/blob/de6760be38b60a3024f1d9054330ff265f0dc619/pokemon_v2/serializers.py), [setup](https://github.com/PokeAPI/pokeapi/blob/de6760be38b60a3024f1d9054330ff265f0dc619/README.md). Data and trademarks require separate rights assessment. No hosted service availability assumed.
- [cyanheads/pokeapi-mcp-server @ 319159d7214785f922b4eb145d6e0d1034b713c4](https://github.com/cyanheads/pokeapi-mcp-server/tree/319159d7214785f922b4eb145d6e0d1034b713c4): [Apache-2.0](https://github.com/cyanheads/pokeapi-mcp-server/blob/319159d7214785f922b4eb145d6e0d1034b713c4/LICENSE); [service](https://github.com/cyanheads/pokeapi-mcp-server/blob/319159d7214785f922b4eb145d6e0d1034b713c4/src/services/pokeapi/pokeapi-service.ts), [Pokemon tool](https://github.com/cyanheads/pokeapi-mcp-server/blob/319159d7214785f922b4eb145d6e0d1034b713c4/src/mcp-server/tools/definitions/get-pokemon.tool.ts), [move tool](https://github.com/cyanheads/pokeapi-mcp-server/blob/319159d7214785f922b4eb145d6e0d1034b713c4/src/mcp-server/tools/definitions/get-move.tool.ts), [filter tool](https://github.com/cyanheads/pokeapi-mcp-server/blob/319159d7214785f922b4eb145d6e0d1034b713c4/src/mcp-server/tools/definitions/find-pokemon.tool.ts). Optional external MCP candidate only; no code copied.
- R06 pinned pkmn/ps modular dex/data API is a candidate for offline experiments. Standalone pkmn/dex GitHub lookup returned 404; do not treat it as verified standalone repository. Use the [dex component within pkmn/ps](https://github.com/pkmn/ps/tree/2a0a161c4ca3cae4f0c7d02e31bcfcb542263601/dex) instead.

## Crucial finding: version filtering is not version fidelity

MCP Pokemon dossier distinguishes form and species, fetches species/evolution/ability records in parallel, includes a version-group-filtered learnset and notices on flavor text fallback. But the inspected code maps current pokemon.stats, pokemon.types, pokemon.abilities, species catch rate and evolution chain into output; getMoveDetails returns current move power/type/PP/category; getTypeMatchups uses current damage_relations. A requested version group does NOT establish that every fact is historical. Gen3 and modern damage class/type behavior are particularly incompatible. Generation introduction filters are not the same as availability in a specific cartridge. Cache TTL and bounded HTTP queries help availability but do not guarantee reproducible historical content. No remote MCP answer may become a legal action or authoritative game state.

Current Pokegent already has private [local extraction](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/local-data.md), [ROM tables](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/firered/rom/rom_tables.py), [wild encounters](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/firered/rom/wild.py), [battle rules](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/ruleset/damage.py) and [dex fallback](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/dex_fallback.py). Keep these sources authoritative for exact admitted title and live mechanics; a general knowledge database complements, not substitutes.

## Proposed knowledge contract

| Class | Source and authority | Use |
|---|---|---|
| live_observed | current validated snapshot / scene | action legality, actual quantities/status, verified progress |
| local_rom | exact ROM + validated artifact | title-specific tables, map/encounter facts, base game rules |
| derived | versioned independent mechanics with explicit input provenance | damage ranges, encounter estimates, team facts, prerequisites |
| discovered | playthrough-scoped verified observation / event receipt | fog-of-war, seen species, visited places, notes vs proof |
| external_advisory | pinned source/API record + retrieval timestamp/hash | strategy, acquisition research, alternative explanations; never sole execution proof |
| unknown | missing, conflicting, unsupported or not visible | explicit refusal/uncertainty, not default modern values |

Proposed key: game family/title, region/revision/ROM profile, generation/ruleset, version group as external mapping only, species/form/record kind and ID namespace, knowledge policy (fair_play/guide), schema/source revision, evidence quality and retrieval identity. Keep native numeric IDs distinct from external national IDs. Negative results distinguish unobtainable, not applicable, not known and service unavailable. Do not cache live RAM or unknown as durable truth. Keep current observer/public redaction.

## Integration sequence

1. Characterize current local fact providers; introduce a read-only game-adapter-owned knowledge interface over existing tables, not a parallel database. R05 governs local artifact manifests.
2. Expand ordinary dex completion acquisition graph: encounter methods, gifts, evolutions, breeding, NPC/link trade, version exclusives, special/event prerequisites, cost/availability. Resolve feasibility and success probes through current objective and adapter; no externally invented target or guarantee of a complete single-cartridge dex.
3. Add optional external adapter for advisory queries, disabled by default; explicit user configuration, fixed allowlisted endpoint, timeout, request/response bounds, cache key with source/version/schema, reproducible snapshot receipts, rate limiting and no secrets/private game state transmitted. Never follow arbitrary links returned by an API; validate host and path. No external call needed to execute ordinary actions.
4. MCP can be consumed separately by strategic agent or wrapped as read-only knowledge provider. Existing R04 agent facade remains distinct from external MCP; no write action from untrusted external strings. Treat free text as data; no prompt/command authority.
5. Publish facts with conflict indicators: local exact ROM takes precedence over current API; record version mismatch, no silent fallback or overwriting. Use bounded context profiles from R03.

## Proposed tests (not executed)

| ID | Test | Expected |
|---|---|---|
| K01 | Gen3 vs modern move category | ROM/ruleset value authoritative; latest API flagged |
| K02 | Past stats/types/ability | exact historical fact or unknown; no current fallback |
| K03 | version exclusive / different revision | distinct feasibility; no false obtainable |
| K04 | species/form/internal index | stable normalized mapping; ambiguity refuses |
| K05 | encounter method and probability | title-specific local distribution and conditions |
| K06 | disconnected network / expired cache | core play unchanged; advisory unavailable |
| K07 | malformed/oversized/redirected response | bounded rejection, no input or arbitrary fetch |
| K08 | cache isolation | revision/provider/schema and knowledge policy separate |
| K09 | hidden information | fair_play projection never leaks guide-only/private fields |
| K10 | objective success | current RAM verification, not external claim |

Coordination: #139 game adapter, #143 support docs, R02 ordinary mechanic gaps, R06 battle comparison, R09 evaluation. No implementation or compatibility promotion. Complete historical API field audit, license/data clearance and live integration proof remain outstanding.
