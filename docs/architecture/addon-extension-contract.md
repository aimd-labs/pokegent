# R11 — Optional add-on ecosystem and policy extension proposal

Status: research proposal, 2026-11-24. Baseline c73774285e5e164d01629ca14a5e8e551e03493f. R02/R03/R04/R07/R09/R10 dependencies. No existing add-on ABI, new permissions, policy or runtime change implied. Ordinary Pokémon mechanics remain core; specialized goals, constraints and external integrations are optional.

## Source evidence

- [PokéBot Gen3 plugin hooks](https://github.com/40Cakes/pokebot-gen3/blob/f14ab79db960315db02333928ba444f9d425b6fc/modules/plugin_interface.py) allow bot modes, listeners, encounter/battle/hatching/evolution events and custom filtering; [encounter](https://github.com/40Cakes/pokebot-gen3/blob/f14ab79db960315db02333928ba444f9d425b6fc/modules/encounter.py) includes repeat detection and filtering, and [static reset mode](https://github.com/40Cakes/pokebot-gen3/blob/f14ab79db960315db02333928ba444f9d425b6fc/modules/modes/static_soft_resets.py) has title-specific targets. GPL-3.0 reference only; no generator/offset/route code copied.
- [PokéBot NDS R08](../research/emulator-backends/nds-automation.md) illustrates hunting across DS families and touch-specific mechanics; MIT code provenance review required, no assets imported.
- [PokeFinder @ 5da107891e3a15bf07c23581f919475f38869aa4](https://github.com/Admiral-Fish/PokeFinder/tree/5da107891e3a15bf07c23581f919475f38869aa4), [GPL-3.0](https://github.com/Admiral-Fish/PokeFinder/blob/5da107891e3a15bf07c23581f919475f38869aa4/LICENSE); [README](https://github.com/Admiral-Fish/PokeFinder/blob/5da107891e3a15bf07c23581f919475f38869aa4/README.md), [WildGenerator3](https://github.com/Admiral-Fish/PokeFinder/blob/5da107891e3a15bf07c23581f919475f38869aa4/Core/Gen3/Generators/WildGenerator3.hpp), [EggGenerator3](https://github.com/Admiral-Fish/PokeFinder/blob/5da107891e3a15bf07c23581f919475f38869aa4/Core/Gen3/Generators/EggGenerator3.hpp) show version/method/lead/filter/seed and separate Emerald vs RSFRLG egg generation. RNG planning is specialized, not required for ordinary capture/breeding.
- [Pokebot-Profiles @ cbd7a509036980bc911c89c77e186e8923d57637](https://github.com/johnnieb333/Pokebot-Profiles/tree/cbd7a509036980bc911c89c77e186e8923d57637), [README](https://github.com/johnnieb333/Pokebot-Profiles/blob/cbd7a509036980bc911c89c77e186e8923d57637/README.md) inventories static, starter, fossil, gift, Safari and legendary scenarios for FRLG/RSE; no recognized license. Scenario names only, no saves downloaded/loaded or copied. Create independent private fixtures (R09).
- [SysBot.NET @ 844c1f31face95bc8e1cfec9c9b7cbd9f7a62895](https://github.com/kwsch/SysBot.NET/tree/844c1f31face95bc8e1cfec9c9b7cbd9f7a62895), [AGPL-3.0 root](https://github.com/kwsch/SysBot.NET/blob/844c1f31face95bc8e1cfec9c9b7cbd9f7a62895/LICENSE); [encounter settings](https://github.com/kwsch/SysBot.NET/blob/844c1f31face95bc8e1cfec9c9b7cbd9f7a62895/SysBot.Pokemon/SWSH/BotEncounter/EncounterSettings.cs), [trade queue](https://github.com/kwsch/SysBot.NET/blob/844c1f31face95bc8e1cfec9c9b7cbd9f7a62895/SysBot.Pokemon/Queues/TradeQueueManager.cs), [README](https://github.com/kwsch/SysBot.NET/blob/844c1f31face95bc8e1cfec9c9b7cbd9f7a62895/README.md) show separate encounter stop policy/counters and external distribution/notification queue. Reference only; no automated online trading or account/service action; respect title/platform rules. Ordinary local trading is core, a remote trade-service queue is optional integration.

Current [addons docs](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/addons.md) name stream, dashboard and System One only; [AddonConfig/Taps](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/addons.py) and [AddonRuntime](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/runtime.py) provide local lifecycle/media/telemetry wiring, not a general gameplay policy ABI. [EventEnvelope](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/events.py) and existing [ledger splits](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/ledger_splits.py) are foundations, not permission to open mutable dashboard or duplicate event stores.

## Classification

| Core / game adapter | Optional add-on |
|---|---|
| Shiny detection of visible encounters; catch, flee, fishing, breeding, static/gift events | repeated shiny hunting strategy, targets/filters and stop condition |
| ordinary faint, healing, storage, legal party changes | Nuzlocke or custom challenge restrictions and run scoring |
| world navigation, objective completion, normal game clocks | speedrun category, split policy, route/time optimization |
| title-specific RNG effects, encounter distribution | explicitly selected RNG research/manipulation policy; no debug writes |
| dex completion, team optimization, EXP/EV, breeding, trading | specialized challenge/competitive farming policy, remote trade queues |
| typed observation, decision, event receipts | external TTS, Twitch/Discord, dashboards, analytics, optional knowledge services, training export |

No core mechanic becomes optional merely because it is outside Elite Four. GamePlugin supplies mechanics (R10); add-on cannot register a new game action/executor without reviewed core/adapter implementation.

## Proposed extension model

Three distinct roles; no arbitrary Python import/plugin sandbox claim:
1. Observer: immutable redacted events/telemetry, opt-in media and bounded queue; slow/unavailable service drops or buffers within limit, never blocks emulator. Observer cannot propose or execute input.
2. Policy: explicit configured scope, reads current public legal options and objective/progress, and may only *request* a legal choice or objective revision through the authorized agent or operator; it cannot write the objective CAS directly, create its own authorization or satisfy irreversible intent. Explicit consent in the current objective remains required. Policy can add *policy* constraints/ranking, never convert illegal/unknown into legal or rewrite game facts. Policy veto reason distinct from engine legality; no silent override or changing the underlying verified option.
3. Integration: external service/client, separate credentials/network/authorization, same safe observation/proposal interface. Read-only default. No incoming chat command executes raw input or activates unreviewed policy.

Proposed manifest: stable addon ID/version/contract version, compatible exact game profiles + required capabilities, role, permitted scopes, observation/knowledge policy, configuration schema and budgets, external endpoints, explicit user consent, evidence/review version. Activate only after required capability checks; missing or unknown => reject activation or degraded read-only mode. Ordinary core and all three deciders work with no optional add-ons. Built-in reviewed policies first; third-party loading/packaging/isolation separate security ADR. No automatic downloads, network, shell, RAM write, raw controller or state path access from a plugin. Private manifest and checkpoint references inert. State/lease/epoch and policy identity must invalidate stale proposals. Multiple policies conflict => explicit configured arbitration or pause; no competing input writers.

## First add-on: Shiny Hunting

Prerequisites: visible shiny status with valid identity/checksum, encounter lifecycle, ordinary legal encounter method and battle/capture, verified resource/catch outcomes, current target availability. Hunting configuration contains title/profile, target species/form if applicable, method, attempt/time/ball limits, permitted resets (off by default), capture vs stop-on-match preference, optional notification. No claim every encounter method is implemented. Start with one ordinary supported wild encounter loop, then fishing, eggs/static/gift/reset strategies only after independent mechanics acceptance.

Proposed state machine: validate -> choose supported acquisition objective -> observe actual encounter -> evaluate filter on fresh allowed facts -> match: protect target, request capture or stop; nonmatch: choose legal next action -> verify result -> update unique encounter/attempt receipt -> repeat within budget. Stop on target match before destructive actions, unknown identity, stale state, unsupported method, exhausted resources or cancellation. Failed catch/escape/no bite is a normal game outcome. No automatic release of non-targets, irreversible item use, save overwrite, reset/reload or RNG write. Opt-in reset needs explicit verified checkpoint, confirmed loss policy, bounded reset/attempt count and recorded lineage; no false duplicate counting across restore. Derived odds/statistics are conditional on title/method and observed data, not guarantees.

## Challenge and speedrun policies

Nuzlocke: configurable rule version (first encounter, area identity, duplicates clause, fainted member handling, exceptions), immutable encounter eligibility receipts and policy constraints on current legal actions. Separate rule violation vs game legality vs technical failure. No mandatory release: lock/box only if authorized and verified; ordinary gameplay remains unaffected when disabled. Resume exact policy version with private checkpoint/ledger; rollback can't erase violation from history. Different games may require different route/area identities.

Speedrunning: category and start/finish proof, game and wall clocks, exact baseline comparison group, checkpoint/rollback policy and allowed tools; use existing ledger splits and first-observed clock semantics. A speedrun strategy is not ordinary shortest-path default. No compare across different ROMs/categories/assistance; preserve failed attempts and interventions.

RNG add-on: separately opt-in local advisory search on title-specific lawful state and bounded budget; do not bundle GPL generators or rely on private secret IDs in public output. No raw write or bypass of game executor; test-only RNG instrumentation separately marked. Default casual/fair-play mode unchanged.

Streaming/TTS/Discord: observer receives bounded public-safe facts and narration; no secrets, private saves, model reasoning traces or hidden facts; queues decoupled, dropped events reported. External viewer/control not allowed by default; chat-driven policy changes require separate explicit authorization and existing guarded interface. Model training exports reviewed/sanitized data with provenance and grouped splits, never automatic raw recordings or secrets.

## Acceptance (proposed, not run)

| ID | Scenario | Result |
|---|---|---|
| A01 | all add-ons off | all ordinary core functions/modes unchanged; no network dependency |
| A02 | unsupported capability/profile | explicit admission refusal, no input |
| A03 | slow/failing observer | bounded queue/drop, no execution backpressure |
| A04 | malicious/stale proposal, including an attempt to set an irreversible objective or intent | refuse, no raw input or game-state mutation |
| A05 | conflicting policies | explicit policy arbitration or pause, no second writer |
| A06 | shiny visible/nonmatch/unknown | valid classification, unknown stops, verified unique attempt count |
| A07 | target match and failed catch | protect target; distinguish actual capture, failed catch, stop |
| A08 | exhausted time/attempts/resources | bounded terminal result; no auto reset/release |
| A09 | resume/reset | explicit permission, accepted state lineage, stale-frame invalidation and no double count |
| A10 | Nuzlocke duplicate/faint rule | correct policy receipts separate from game legality |
| A11 | speedrun clock rollback | ineligible comparison with explanation; no erased attempts |
| A12 | external service/chat input | no implicit control, redaction, bounded credential/network scope |
| A13 | version/schema mismatch | explicit refuse or reviewed migration; no silent policy rewrite |

Implementation sequence: core encounter/fishing/shiny facts and typed events (#176/R02), R10 capability seam, reviewed observer contract, first local shiny policy, one challenge policy, then integrations and other specializations. No separate duplicate issue yet; senior review decides scope. No foreign implementation, save, asset or dependency adopted. No native test or policy activation.
