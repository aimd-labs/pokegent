# R12 — OSS maintenance and release governance

Status: proposed, 2026-11-24. Runtime baseline c73774285e5e164d01629ca14a5e8e551e03493f. No workflow, settings, labels, source code, dependency, release or permission changes. Existing maintainers own approvals, merges, deployment and org-admin settings.

## Current evidence and gaps

- [CONTRIBUTING](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/CONTRIBUTING.md), [AGENTS](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/AGENTS.md), [SECURITY](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/SECURITY.md), [PR template](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/.github/PULL_REQUEST_TEMPLATE.md), issue templates, Code of Conduct, MIT license and source lock exist. Contributions use Python 3.11, synthetic ROM-free tests, maintainer review and no bot merges. Fork PRs run without secrets with read-only permissions. Preserve this baseline.
- [CI](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/.github/workflows/ci.yml) has public-checks (lint, compile, import, pytest, game-data guard), native-helper (six synthetic helper tests without skips) and public-startup-latency (<30-second public factory startup). Actions checkout/setup-python pinned by SHA, checkout credentials not persisted, workflow permissions contents:read, per-job timeout and concurrency configured. These are workflow facts, not proof of required branch checks or native gameplay.
- GitHub releases API reports [v0.1.0](https://github.com/aimd-labs/pokegent/releases/tag/v0.1.0) published 2026-11-19 with wheel and SHA256SUMS. Issue #144 wording describes preparation, but release already exists; improve next release and verify existing artifact separately. No downloaded artifact or hash/attestation verification in this audit.
- [Wheel regression](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_wheel_packages_references.py) builds/installs in a separate venv, tests resource loaders and negative case; dependency installation uses ranges/build isolation. Good package integrity test, not reproducible artifact proof.
- CODEOWNERS and separate dependency/security workflows not identified in pinned tree. Branch protection GET returned 403 from integration; required checks, approval counts and org rules **unknown**. Do not infer settings from CI or change settings. #145 explicitly requires org-admin/Chief coordination.
- [OpenSSF Scorecard README](https://github.com/ossf/scorecard/blob/789a810815a4398abf14b10898b111d3ced5d94d/README.md) and [check reference](https://github.com/ossf/scorecard/blob/789a810815a4398abf14b10898b111d3ced5d94d/docs/checks.md) are ongoing security practice references (README inspected; detailed checks are follow-up). [CodeQL Action README](https://github.com/github/codeql-action/blob/776237d428f23fefc3d9e0fe1962921cbcd53e17/README.md) describes default/advanced setup and license distinction. No Scorecard/CodeQL scan executed, no fabricated score or claims of vulnerabilities.

## Existing maintenance work (no duplicate issues)

| Issue | Proposed next small slice | Acceptance / owner |
|---|---|---|
| [#140](https://github.com/aimd-labs/pokegent/issues/140) CODEOWNERS | Maintainers supply verified handles/teams and access; define runtime, readers/profiles, provider, CI/security, docs ownership with coverage check | Review requests go to actual authorized owners; no invented owner |
| [#142](https://github.com/aimd-labs/pokegent/issues/142) security | First dependency review and reviewed CodeQL default/advanced path; pin actions and minimal permissions; parser fuzz/property suite separately | Safe fork CI, no privileged contributor execution; checkable results, alert triage |
| [#143](https://github.com/aimd-labs/pokegent/issues/143) compatibility/onboarding | Publish exact game/revision/backend/host and evidence labels from R01/R05/R09; one current setup path, deprecations explicit | Reproduce clean install and narrow native smoke; no full-game promise |
| [#144](https://github.com/aimd-labs/pokegent/issues/144) reproducibility | Rebuild wheel/sdist twice in isolated pinned toolchain; dependency lock/update proposal, provenance/SBOM, artifact digests and attestations | Compare hashes or explain nonreproducible bytes; independently verify installed release and test receipts |
| [#145](https://github.com/aimd-labs/pokegent/issues/145) required checks | Chief/org admin verifies protection and required names; consider all three existing checks | Actual settings receipt, no integration change here |
| [#146](https://github.com/aimd-labs/pokegent/issues/146) ledger backfill | Narrow mapping of exact legacy event spelling with original receipt | Idempotent historical fixture, reject unrelated malformed event atomically |
| [#148](https://github.com/aimd-labs/pokegent/issues/148) debug tooling | Existing pin accepted; lnav installation through ops and one actual first-bad-transition trace | Redacted trace and minimal regression; no new reference checkout |

Operational blockers #178/#180/#165 remain higher priority; #106 explicitly paused. R03/R09 cover routing and native proof. Do not start new overlapping worker lanes. No issue statuses, assignees, labels or branches of other developers changed.

## Contributor and agent workflow

1. Before work, read AGENTS/CONTEXT/contracts, find existing issue, record exact baseline and scope, designate one author/owner lane. Research proposal is not authorization to change production. Pin external sources and check actual licenses/data rights. MIT-compatible code adaptation only after review; GPL/AGPL/noncommercial/unlicensed references are behavioral research; no data or asset copying.
2. Focused PR, clear behavior and failure reproduction, smallest synthetic regression, unchanged safety/provenance. No ROM, saves, extracted assets, logs, machine secrets or private memory in public artifacts. Redact evidence; actual native results privately reviewed.
3. Author checks exact commands and outcomes, skips and limitations. Independent senior reviewer checks contracts, changed files, test assertions and evidence. CI green is necessary, not sufficient. Runtime change deployment requires separate maintainer approval. No agent merges or pushes to main.
4. For current stacked research chain only, intermediate bases are prior research branches to avoid duplicate diff, a deliberate exception to the ordinary main-target guideline. Review in sequence from #182 through #192 then R12. Do not delete a base branch until dependent PR retargeted. After a squash/rebase merge, rebase or cherry-pick reviewed incremental changes onto main and retarget next PR; recheck actual diff, exact head and CI. GitHub automatic retargeting must not be assumed. Alternative for senior: review all incremental PRs, then assemble reviewed changes into one main-targeted integration PR; explicitly close superseded proposals. Do not merge both approaches.
5. Separate approved research (design acceptance) from implementation acceptance; current contracts remain binding, proposals don't automatically change runtime. Convert accepted slices into existing issue comments/assignments or new scoped issues only as needed, no duplicate backlog.

## Release and verification proposal

- Pin reviewed source commit/tag, Python/build toolchain, dependencies and optional native artifacts, host matrix. Maintain lock/update policy for build/test dependencies with hashes and reproducibility. For supported installs use reviewed dependency updates, not arbitrary latest.
- Build wheel and sdist in clean isolated environment; run existing package/resource tests plus install-from-artifact checks. Validate metadata/version, reject distributed game data and secrets, compare independent artifact hashes; document exceptions rather than label nonidentical builds reproducible.
- Record SHA256SUMS, source commit, build workflow identity, package inventory/SBOM, attestation verification procedure and release note; use least privilege, short-lived signing credentials only in trusted protected release job, never contributor PR. Review native helper/optional dependency redistribution separately.
- Release evidence manifest: exact commit/artifact hashes, CI job names/conclusions/URLs, host/backend/game/profile and R09 test receipts, known limitations, dependency lock and provenance. Synthetic CI and startup tests cannot promote game compatibility. Keep candidate vs verified vs unsupported clear.
- No automatic release, deployment, approval or branch setting changes. Scorecard guidance is a checklist, not target score; measure after safe setup. Maintain vulnerability reporting path, triage dependency and parser findings, remediate separately; no credential logging or private data in scan outputs.

## Suggested post-review implementation order

A. Review/merge research, validate registry/schema and evidence links; preserve exact versioned proposals. B. Resolve existing P0 incidents with deterministic regression and native owner receipts. C. #132–#138/R09 deterministic route and evaluation, FireRed native scenarios/campaign. D. #139/R05/R10 second-title seam and verified profile. E. Close ordinary capability gaps, local knowledge, later games. F. R11 optional policies after required mechanics. Maintenance #140/#142/#143/#144 can run in independent narrow lanes subject to senior review and existing coordination. This is a prioritization proposal, not a calendar promise.

## Senior review index

- R01 [PR #182](https://github.com/aimd-labs/pokegent/pull/182) — [Project mapping](../architecture/current-state.md): Initial baseline only; full import/test assertion inventory remains.
- R02 [PR #183](https://github.com/aimd-labs/pokegent/pull/183) — [PokéBot Gen3](game-mechanics/pokebot-gen3.md): Selected source crosswalk, no native reproduction.
- R03 [PR #184](https://github.com/aimd-labs/pokegent/pull/184) — [System One / hybrid](system-one/jev-comparison.md): Architecture proposal and 24 not-run cases.
- R04 [PR #185](https://github.com/aimd-labs/pokegent/pull/185) — [Nous upstream](agent-frameworks/nous-upstream.md): 34-file semantic crosswalk, not historic diff.
- R05 [PR #186](https://github.com/aimd-labs/pokegent/pull/186) — [Profiles/decompilation](game-mechanics/decompilation-profiles.md): Five selected source pins; no new ROM admission.
- R06 [PR #187](https://github.com/aimd-labs/pokegent/pull/187) — [Battle/PKHeX](game-mechanics/battle-intelligence.md): Independent oracle/test design; no executed vectors.
- R07 [PR #188](https://github.com/aimd-labs/pokegent/pull/188) — [Knowledge layer](knowledge-sources/knowledge-layer.md): Local-first authority; no hosted API calls.
- R08 [PR #189](https://github.com/aimd-labs/pokegent/pull/189) — [Nintendo DS](emulator-backends/nds-automation.md): Selected DS references; no backend choice.
- R09 [PR #190](https://github.com/aimd-labs/pokegent/pull/190) — [Evaluation](../benchmarks/evaluation-protocol.md): Manifest proposal, no benchmark result.
- R10 [PR #191](https://github.com/aimd-labs/pokegent/pull/191) — [GamePlugin/backends](../architecture/game-plugin-contract.md): Contract proposal, no runtime change.
- R11 [PR #192](https://github.com/aimd-labs/pokegent/pull/192) — [Add-ons](../architecture/addon-extension-contract.md): Observer/policy/integration proposal, no activation.
- R12 (this PR) — [OSS maintenance](oss-maintenance.md): Governance and handoff proposal; settings unchanged.

All twelve initial deliverables are proposed; no full exhaustive source audit or runtime validation implied. Check outstanding reference candidates in registry, R01 detailed audit gaps, R05 exact variant/DS+ proof, R06 differential vectors, R07 historical-field fidelity, R08 backend conformance, R09 schema/actual benchmarks, R10 adapter ABI, R11 policy safety, R12 settings/release evidence. Reviewers should reject unsupported claims and treat candidate thresholds as proposals.

At inspection immediately before R12, PRs #182–#192 were open; public workflow runs for #182–#190 reported success, #191/#192 in progress. This is a timestamped snapshot; verify current head CI before review/merge. No native tests run in research session; no release promoted. Branch protection unavailable via integration (403); no claim of effective rules.
