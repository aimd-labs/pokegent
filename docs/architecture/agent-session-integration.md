# Proposed agent, backend and playthrough integration seams — R04

**Status:** research proposal, not a frozen API or implementation. Baseline `c73774285e5e164d01629ca14a5e8e551e03493f`. [Upstream audit](../research/agent-frameworks/nous-upstream.md), [R03 routing](decision-routing.md). Coordination: [#139](https://github.com/aimd-labs/pokegent/issues/139), [#133](https://github.com/aimd-labs/pokegent/issues/133), [#135](https://github.com/aimd-labs/pokegent/issues/135), [#138](https://github.com/aimd-labs/pokegent/issues/138), [#143](https://github.com/aimd-labs/pokegent/issues/143), R05, R09, R10, R11. Maintain existing contracts and no bundled game data. No new runtime dependency, server, endpoint, schema or privilege is introduced by this document.

## 1. Keep the boundaries that work

```text
    External agent / optional local client / System One
        | inspect / choose / objective CAS
    Existing decision bridge + runner + router
        | registered legal option / verified result
    Perceiver + Executor + single EmulatorActor
        | game-owned snapshots, codecs, rules, options, verifiers
    admitted GamePlugin (one exact game/revision per run)
        | platform-specific read/input/state capabilities
    backend (mGBA today; PyBoy candidate; later others)

    Local playthrough lifecycle (proposed, separate control plane)
        | validated start/stop/resume, one lease, checkpoint receipt
    existing supervisor + private files + run ledger
        | read-only status / telemetry / observer
    dashboard, optional external integrations
```

The diagram is conceptual; it does not specify import layering. The actor is the only emulator writer, the game plugin owns mechanics and evidence, the router owns decision assignment, objective session owns accepted revision, the supervisor owns process/segment lifecycle. A client is not a second executor. Observers never gain input permission. Keep R03's three modes independent; neither MCP nor Hermes is required to run Pure System One or agent mode. No plugin or named-session UI bypasses the existing execution contract.

## 2. Characterize before extracting GamePlugin

Current seams already exist:
- [FireRedAdapter](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/firered/reader/adapter.py#L10-L64) bundles reader/UI/battle/map providers and rules, but its registration method is a no-op placeholder. Do not claim active generic plugin loading.
- [build_runner](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/app.py#L650-L856) still constructs FireRed menu resolver, ROM tables, shop observation, family registry and game-specific readers; [open_emulator](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/app.py#L1278-L1307) hardcodes profile + mGBA.
- [Perceiver](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/pipeline.py) and [ObjectiveTracker](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/tracker.py) depend on FireRed StateSnapshot. [Objective validation](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/validate.py) uses FireRed map/constants. [Navigation models](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/navigation/models.py) encode map-group/number coordinate structures. [Scene registry](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/actions/__init__.py) and [catalog](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/state/catalog.py) already enforce unique ownership. Preserve that, not a plugin that can register arbitrary inputs.
- [Actor](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/emulator.py#L1286-L1348) accepts region definitions but defaults to GBA. [Actor save/load](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/emulator.py#L1617-L1656) assumes bytes API. PyBoy wrapper is retained but not an admitted game.

Proposed narrow contracts (names illustrative, R10 formalizes):
| Boundary | Required semantics | Refusal / evidence |
|---|---|---|
| ROM identity/admission | exact hash + game/revision/language/profile/schema; profile and local data digest | unknown/mismatched ROM stops before input |
| Backend | platform capabilities, bounded frame/input/atomic read/state API, restore epoch, immutable observation | unsupported operation fails explicitly, no fake capability |
| Game snapshot | shared typed health/identity and applicable state projection, adapter-private raw facts | unknown/inapplicable explicit; no invented Gen3 fields for Gen1 |
| Registration | scene catalog, unique detectors/builders, registered executor/postcondition, versioned coverage | duplicate owner or unresolved action refuses; no arbitrary code import from config |
| Navigation | adapter-owned map/edge identities, geometry, gate/transport/arrival proof | no route inferred from missing collision or unrelated map coordinates |
| Rules/knowledge | title-specific mechanics, local data, observation visibility, optional advisory external knowledge | no unreviewed hidden/private data, no remote dependency for baseline |
| Objective/progress | game-specific validation and success probes behind common session/CAS | proof required from fresh usable state; unknown cannot complete |
| State restore | backend/revision/format/identity and validated private state, monotonic restore epoch | wrong save/unknown epoch refused; never reuse pending old frame |

Do not define a mega-interface that every title must implement; use declared capabilities and exact applicable data. Keep adapter data separate from generic action ownership and choice contracts. New game-specific fields must be versioned and namespaced; do not silently modify v1.x schemas. Test extension, not just boot: a second title must supply legitimate snapshot, scene, legal actions, verified movement and battle transition plus save/resume, without new game-specific branches in generic routing/executor. Preserve current FireRed regression behavior and supported native startup target. Exact shape and any changed imports require ADR and reviewed source slices.

### Order

1. Characterize FireRed Rev0 through current synthetic fixtures and private native acceptance; pin behavior and data provenance.
2. Extract one minimal adapter-owned seam at a time (selection → reader composition → catalog/registrations → objective/world/rules). Introduce fake adapter test with no FireRed imports on its path only after import inventory; avoid speculative rewrite.
3. Follow #139: another FireRed revision/LeafGreen, then Emerald, verify distinct game/revision. Reuse Gen3 mechanics where independently valid, do not assume all addresses/flags/menus compatible. No automatic admission of other ROMs.
4. PyBoy actor conformance (R04 audit concrete gaps); Gen1 exact profile, reader, scene and input tests; then Red/Blue variants, Yellow and Gen2 independently. Do not assume PyBoy .gb/.gbc support equals game capability.
5. Future DS/3DS/Switch/native/hardware requires explicit backend capability/transport contracts; do not hardcode GBA addresses, fixed screenshot dimensions or backend-specific save format in core.

## 3. Versioned agent-facing facade, not raw-input REST

Current [server contract](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/server.md) and [connect-your-agent](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/connect-your-agent.md) suffice for a minimal portable client. Optional SDK/MCP work should wrap actual supported behavior, not expose internals. A versioned facade is a proposal under #139; preserve current endpoint behavior and use compatibility tests before adding API versioning.

| Agent operation | Current contract | Proposed facade behavior |
|---|---|---|
| Inspect available actions | GET /frame?wait=0 (or bounded poll) | Return current guarded state/options/frame identity, status object if absent; distinguish normal vs expanded selectable scope |
| Choose | POST /choose exact frame_id + option_id | Only inspected ID; 202 queued, not success. 409 stale → discard/reinspect, never rebind same choice automatically. No raw inputs. |
| Planning facts | GET /briefing | Versioned, local/redacted facts and proven capabilities; no hidden addresses or unsupported assumptions |
| Objective | GET/POST /objective, existing CLI/CAS | Validate target and success probes, explicit expected revision; protect newer objective |
| Observe result | Subsequent frame feedback / events | Match request/decision identity; separate applied/verified/uncertain/failed and durable progress |
| Status/screenshot | GET /status, GET /screenshot | Preserve existing local behavior; screenshots advisory, not state proof |
| Interrupt | POST /interrupt | Request existing safe boundary interruption only; not lifecycle stop or raw reset |
| Start/stop/restore/delete | Not game-input bridge operations | Separate local owner-managed lifecycle; no automatic legacy endpoint compatibility |

SDK initial deliverable: tiny Python client using existing request path and explicit timeout, typed result/queued status, versioned feature discovery only when supported, mock test examples for wrong/stale frames, no pending frame, interruption, invalid objective and no credentials. Optional MCP tools can wrap inspect/choose/briefing/objective/status with exact schemas and same authorization; tool descriptions distinguish state-reading vs mutating choice/objective. No provider pin, implicit write tool, full filesystem read, arbitrary emulator args or raw /action. An agent can interact through current CLI without installing an SDK. Keep loopback unauthenticated boundary local; no automatic public expose/tunnel. A remotely accessible control plane would require a separate auth/scope/threat-model review.

### Persistent agent clients

Current [owner review skill](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/skill/pokegent/SKILL.md) and [owner status](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/ops/hermes/owner_status.py) separate persistent strategy session, readiness, review receipts and per-choice consumer. Do not reuse upstream Hermes subprocess or approval bypass behavior. Optional adapter (Hermes, custom, etc.) stores a private explicit agent binding per playthrough and uses exact frame tokens. One active controller lease; stop/cancel stale worker before switch, no unbounded duplicated work. Owner review is not a pending-choice consumer. Model/context continuity must never authorize a new action or override accepted game state. Separate model call budgets, actual token/cost metrics and operator intervention under R03/R09.

## 4. Proposed named playthrough layer

A playthrough is a long-lived user-selected campaign, distinct from a single run/segment and a saved-state artifact. Existing [supervisor](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/live-supervisor.md) uses a private LIVE_HOME, lock, current.state pointer, run output and objective; [ledger](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/run-ledger.md) tracks saves and segments, facts and receipts. Keep them authoritative for lifecycle and observed evidence. Avoid adding a competing game-session database or a second supervisor.

Candidate local manifest (field names conceptual, not an accepted schema):
| Field | Meaning |
|---|---|
| playthrough_id, label | Stable generated opaque ID and display-only user label |
| game/profile/revision/language + ROM digest | Exact admitted identity; no ROM bytes or public paths |
| backend/state format + version, release/config digest | Compatibility and reproducibility identity |
| save_id, active segment reference | Explicit mapping to existing ledger identity; ledger's mode means casual/speedrun, not model routing mode |
| validated checkpoint receipt | State hash, bytes, originating run/restore provenance, acceptance, relative reference in private scope |
| objective revision / mode | Current objective store remains authority; manifest stores pointers/receipts, not competing values |
| controller/agent binding | Private optional opaque adapter/session binding, never public or required |
| lifecycle status / generation | Requested/running/quiescing/stopped/blocked, durable transaction receipt, ownership epoch |

No generated data, secrets, private agent transcripts or ROMs in git. Use safe internal ID grammar (labels not paths); reject absolute/traversal/symlink or foreign path references; canonical private root and bounded reads; atomic write/replace and crash-consistent recovery. Exact backend/ROM/version/state match before restore, native/RAM acceptance after restore. Selection is explicit, not latest by mtime/name; missing or incompatible selected state refuses without switching or attaching new identity to old game. Save receipts never overwritten until accepted. Old frame tokens, queued actions, objective proposals and outstanding model responses invalidated across ownership generation. Keep rollback as explicit operator choice with lineage and progress-loss receipt, not auto recovery. A named session's agent binding must not be reused for another playthrough without explicit authorization. Multi-process concurrency only after isolation and port/resource policy; initial scope one active writer, no hot swapping while a macro is running.

### Bounded implementation proposal

- S0: read-only catalog/list/inspect of explicitly configured private playthrough roots and existing accepted receipts; distinguish unknown/empty/corrupt/incompatible. No start or delete.
- S1: create isolated private directory/manifest and explicit cold boot request using existing CLI, preserve user's current run; no implicit new game overwrite.
- S2: explicit local stop/quiesce, save/verify, then resume chosen compatible checkpoint through existing supervisor; transaction can recover without starting two writers. If preparation fails, old accepted reference remains intact, new session not marked running. No service changes from dashboard.
- S3: optional read-only dashboard session picker/info, then reviewed local owner controls only if requested. No legacy DELETE or arbitrary filesystem operations; deletion/archival separate, with explicit owner intent and safeguards.
- S4: optional persistent controller binding, detach and rebind after state validation, private memory isolation. No new mandatory agent dependency.

Proposed acceptance (not executed):
| Case | Required result |
|---|---|
| Two playthroughs same ROM/different objectives | Distinct save/segment/controller identities, no state or memory leakage |
| Unknown ROM, wrong backend/revision or corrupt state | No input, no new active playthrough, no fallback guess |
| Missing selected checkpoint | Explicit refusal; no activation on old emulator |
| Interruption during save/switch | Previous accepted receipt intact or explicit blocked state; no split-brain |
| Old frame token/agent result after restore | Rejected, no input; new identity and fresh snapshot |
| Malicious display label/path/ref, symlink, oversized manifest | Bounded refusal, no external file read/write/delete |
| Owner or provider unavailable | Current typed safe handling; no silent fallback brain |
| Read-only catalog/observer | No lifecycle or emulator mutation, private refs redacted |
| Replay/backfill ledger | Same stable identities, idempotent events, no false badge/campaign proof |

R09 supplies private scenario manifests and multi-seed acceptance, R12 release/security gates. Use [existing checkpoint/state code](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/app.py#L1310-L1436) and [supervisor lock](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/supervisor.py#L431-L475); do not create a second state validator. Current source uses explicit opt-in checkpoint recovery; preserve default no fallback in supervised runs.

## 5. Scope and handoff

This is a design direction, not final protocol/manifest schema, native support claim or implementation assignment. R05 provides independent profile/ROM facts; R10 formalizes small GamePlugin/backend seams against actual code; #139 already tracks ordering and Python SDK/MCP; R09 tests; #143/#144 cover published support and release evidence. Named-playthrough catalog is a proposed feature, to be scoped with maintainers before filing a separate issue. Existing P0 incidents and #106 pause remain unchanged. No code adoption, configuration, workflow, dependencies, external calls or execution. The current FireRed runtime remains authoritative.
