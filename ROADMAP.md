# Pokegent roadmap (proposal)

The goal is a complete Pokémon tool for arbitrary agents, not a fixed walkthrough bot. FireRed is the first reference implementation. Ordinary game mechanics are core capabilities; game adapters implement title-specific details; optional add-ons implement specialized policies or integrations. Existing v1.x contracts, maintainer review, no bundled game data, single-writer execution and fail-closed legality remain binding.

## Work packages

| WP | Priority | Deliverable | Acceptance / dependency |
|---|---|---|---|
| R01 | P0 | Pinned architecture baseline, capability map, registry, gaps | Initial docs delivered; close detailed import/test/assertion inventory separately |
| R02 | P0 | PokéBot Gen3 source audit | Pin source commit, map mechanics to capability IDs, license-safe reference notes, missing cases and tests |
| R03 | P0 | Jev/System One research | Compare legal-choice payloads, battle facts, recovery, end-to-end claims, offline/ROM benchmarks |
| R04 | P0 | Nous upstream divergence | Trace reusable MIT code and local API/actor/session differences; respect provenance pins |
| R05 | P1 | Decompilation/game profile research | Document symbol/data extraction, ROM identity/revision support, generation-specific behavior; no game datasets shipped |
| R06 | P0 | Showdown, damage-calc, PKHeX research | Version-aware battle and data contracts, differential tests, license review |
| R07 | P1 | Knowledge layer | Local-first versioned knowledge; optional external API/MCP; no remote dependence for base mechanics |
| R08 | P1 | DS automation research | Compare archived PokéBot NDS, DS emulators, Gen 4/5 state/controls; no premature backend commitment |
| R09 | P0 | Reproducible evaluation | Public synthetic corpus, private native scenario manifest, independent campaign outcomes for all modes; existing ledger as basis |
| R10 | P1 | Game plugin and backend contracts | Characterize FireRed, prove minimum seam on second title and alternate backend; compatibility gates |
| R11 | P2 | Add-on extension research | Policies use public core capabilities; shiny, Nuzlocke, speedrun, RNG, streaming integrations |
| R12 | P0/P1 | OSS maintenance | Existing issues for reproducibility, security, contribution ownership, release/compatibility evidence |

## Initial research delivery status (2026-11-24)

All twelve initial documentation packages have been prepared as a stacked review proposal. See [senior review index](docs/research/oss-maintenance.md#senior-review-index) for each incremental PR and outstanding validation. This is not completion of implementation, exhaustive source audits, native acceptance or a release gate. No support statuses are promoted. Review and merge in order, retarget/rebase as necessary; maintainer owns merge and deployment.

## Next implementation plan (v0.1.1 FireRed stability)

Planning target is v0.1.1 FireRed stability; this is not a release, deployment or a claim that any completion gate or 90% target is achieved.
Order: #179 payload (done, `cc46`) → #181 Fly config/small existing-offering fix → #180 correlated warp/exit wrong-transition → #133 concrete cycle; then #176/#168/#162 as small PRs.
Runtime owner is Hermes with one child; pg-lead gives one review per green change; no concurrent duplicate author. Issue #106 stays paused.
Acceptance: no unauthorized input, exact-source public CI and startup budget unchanged, and owner menu, save-resume, Fly and warp canaries run separately.
No new game, plugin or DS work. See [senior disposition](docs/research/senior-disposition.md).

## Milestones (no calendar promises)

1. **Research foundation:** R01 initial mapping, R02–R07 research notes pinned to source revisions; prioritize current live blockers in parallel with research.
2. **FireRed evidence:** resolve reproducible P0 blockers, run native scenarios and intro-to-credits campaigns; compare agent, hybrid and pure System One independently. Follow existing issues #132–#138, #165, #168, #176, #178, #180, #181; see gap analysis for links. Record cost, decisions, forced choices, verified progress, handoffs, stuck events, wipes and p50/p95 decision latency with consistent denominators.
3. **Game plugin seam:** address #139 after characterization; choose a second game based on research (LeafGreen or Emerald are candidates, not commitments). Verify common interfaces and variant rules rather than copying FireRed addresses.
4. **Complete ordinary mechanics:** close capability gaps beyond the Elite Four plan, with version-specific constraints. Main-story completion alone is not full coverage. Work on Gen 1/2 and 4/5 after interface proof; research Gen 6+ and spin-offs without support promises.
5. **Optional ecosystem:** design policy/integration extension API only after capability and execution seams; prove a shiny hunting policy and one challenge policy without bypassing legality.

## Release/compatibility gate

Report support for an exact game, region/revision, backend and host. Maintain capability-level implementation and verification independently. Distinguish boot, readable state, basic actions, complete story, ordinary postgame mechanics and repeated reproducible autonomous play. No title is declared fully supported merely because an emulator starts it. All public checks, maintainer review and source notices remain mandatory. Keep planned features explicitly marked as such.

See [research methodology](docs/research/README.md), [current state](docs/architecture/current-state.md), [capabilities](docs/capabilities/taxonomy.yaml), [gap analysis](docs/research/gap-analysis.md) and [existing issues](https://github.com/aimd-labs/pokegent/issues).
