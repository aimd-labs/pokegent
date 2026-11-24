# Research and reference registry

This directory coordinates research before implementation. The project goal is an agent-agnostic framework covering ordinary Pokémon game mechanics across titles, with optional specialized policies and external integrations. The runtime contracts remain normative; research notes and roadmap entries are proposals unless accepted through review.

- [Architecture baseline](../architecture/current-state.md): pinned current-state audit.
- [Reference registry](registry.yaml): candidates, provenance and intended research use.
- [Capability taxonomy](../capabilities/taxonomy.yaml): ordinary gameplay/core vs optional extension scope.
- [Compatibility](../capabilities/compatibility.yaml): per-title evidence; never infer support from emulator availability.
- [Gap analysis](gap-analysis.md): prioritized findings and existing issue links.
- [Roadmap](../../ROADMAP.md): proposed dependency-ordered work packages.

## Completed research slices (proposals)

- [R02: PokéBot Gen3 source audit](game-mechanics/pokebot-gen3.md) and [capability crosswalk](game-mechanics/pokebot-gen3-capabilities.json). Exact upstream pin, selected source evidence, ordinary mechanics vs policies, no copied GPL code.
- [Open issue review — 2026-11-24](issue-review-2026-11-24.md): snapshot of 22 issues, coordination and proposed priorities; not a live board.

## Research workflow

1. Register source URL, repository identity, license signal, scope, and inspection level. Before a detailed audit pin the exact source commit; a README or metadata check alone is not a code audit.
2. Map a concrete mechanism or test to stable capability IDs; record source path, symbol or line, commit, game/revision, assumptions, limitations and reproduction steps. Distinguish facts, claims, hypotheses and proposals.
3. Compare with the baseline implementation and existing issues. Record missing behaviors, not just missing modules. Prefer small reproducible scenarios to speculative architecture changes.
4. Choose `REFERENCE` (study behavior/interoperability facts and independently implement), `DEPEND` (external dependency after review), `ADOPT` (compatible code with notices), `ADAPT` (compatible adaptation with notices) or `REJECT`. Registry suggestions are preliminary, never permission to copy. Review actual license texts, dependencies, data rights and attribution first. GPL/AGPL code must not be copied into a MIT-only combined distribution without a compatible licensing decision. Separate-process integration is not an automatic licensing exemption. Repositories without a clear license grant are reference-only. Keep existing `source-lock.md` and `THIRD_PARTY_NOTICES.md` authoritative for incorporated sources.
5. Propose acceptance tests, quantify decision cost/latency where relevant, and link an existing issue or create a focused new one only if necessary. Record accepted architectural choices in an ADR (future work); implementation requires its own reviewed PR.

## Evidence rules

Record implementation status separately from test evidence. `present` means an implementation surface was identified, not that all subcases work. `partial` means known limitations or narrower scope; `gap` is a documented missing behavior; `unknown` is unassessed, not absent. `not_applicable` requires a game-specific reason. Evidence labels: `path_inventory`, `source_inspected`, `test_path`, `documented_native_smoke`, `historical_ci`, `reproduced_scenario`, `reproduced_campaign`, `repeated_regression`. A `test_path` is not an assertion audit. A green public CI is not ROM-backed proof. Per-case native evidence must name ROM revision, backend/version, fixture origin, objective, seed or replay, mode, and actual outcome; keep private game data outside the public checkout.

## Scope rules

All normal game mechanics, including Pokédex completion, team planning, trading, breeding and training, belong to core capability scope. Adapters implement title-specific behavior; core may provide reusable algorithms and planning facts. Objectives express intent; ordinary strategies need not be hardcoded. Optional extensions add specialized policies (shiny hunting, Nuzlocke, speedrun, RNG manipulation) or external services. Reading shiny status is core; hunting policy is optional. A policy never gains a second input writer or bypasses executor legality and postconditions. Offline/local data should remain sufficient for supported base mechanics; an external knowledge service is optional.

## R01 limitations

The pinned tree was fully enumerated and selected modules inspected. This is not a full source/AST, dependency, branch-protection, test-assertion or live incident audit. No local emulator or test run was performed. The existing baseline public CI was observed successful. Registry candidates marked `candidate` must be verified before adoption; other entries reflect README/metadata-level inspection, not license clearance. No claims of game completeness are made.
