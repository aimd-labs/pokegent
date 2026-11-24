# Proposed decision architecture — R03

**Status:** proposed, non-normative. Existing [contracts](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/contracts.md), [CONTEXT](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/CONTEXT.md), objective and executor contracts are authoritative. Baseline `c73774285e5e164d01629ca14a5e8e551e03493f`. Research: [Jev comparison](../research/system-one/jev-comparison.md). Coordination: [#132](https://github.com/aimd-labs/pokegent/issues/132) (router/provenance), [#133](https://github.com/aimd-labs/pokegent/issues/133) (recovery), [#134](https://github.com/aimd-labs/pokegent/issues/134) (calibration), [#135](https://github.com/aimd-labs/pokegent/issues/135) / [#138](https://github.com/aimd-labs/pokegent/issues/138) (campaign), [#139](https://github.com/aimd-labs/pokegent/issues/139) (game interface). No runtime or config changes in this PR.

## 1. Current source findings (not runtime reproductions)

| Area | Evidence | Implication |
|---|---|---|
| Runner's bypass | [loop.py: _handle_ready](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/loop.py#L1353-L1400) directly handles a singleton classified as deterministic/autoplay-safe/no-input; action-bearing options otherwise go to decider. | Don't equate every single available option with authorized autopilot. Historical contract wording and comments may overstate generic forced routing. |
| System One local singleton | [SyncDecider.decide](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/provider.py#L231-L287) builds the plan then returns local forced ChoiceResult with confidence 1.0 if one option. | No HTTP call, but internal result is not statistical model confidence. Exclude from model calibration. In agent mode don't silently change its authority. |
| Hybrid route veto | [HybridDecider.decide](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/hybrid.py#L210-L389) normally asks System One first; then [_route_step_veto](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/hybrid.py#L391-L448) can substitute exactly one next route step for an off-route navigation choice. Risk/irreversibility/ambiguity matter. | A route replacement currently still consumes a provider call, and some paths attribute it as system1. Move only a **proven eligible subset** before provider; retain veto for non-proven residual cases until reviewed. Never substitute for meaningful heal/item/battle decisions. |
| Confidence | [policy.py](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/policy.py#L93-L148) and [provider record](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/provider.py#L404-L459) describe observe-first/disabled calibration gate. Hybrid independently compares confidence <= per-scene threshold, default 0.35, and escalates missing confidence (unless a qualifying route veto). | Telemetry "gate disabled" refers to provider calibration, not hybrid authority. Separate claim quality from policy admission; fix attribution/wording in a reviewed follow-up. A 0.35 threshold has not been shown calibrated for current FireRed. |
| Chat vs choice | [docs/system-one.md](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/system-one.md) says openai-chat returns no probability or confidence; [hybrid](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/hybrid.py#L251-L271) handles missing data. | Default chat hybrid will frequently hand off; no fake confidence, logprobs or guessed probabilities. Need explicit empirically validated scene eligibility for confidence-less providers. |
| Handoff | [configuration](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/configuration.md#L10-L57) and [hybrid](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/hybrid.py#L193-L389) bound escalated agent handoffs (default 300 s). Earlier provider/system-one docs still say indefinite. | Correct documentation separately; do not assume infinite waits in current hybrid. Pure external-agent waits are a distinct mode/contract. Timeout must pause without reusing a rejected choice. |
| Questions | [questions.py](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/questions.py#L116-L128) default max_state_tokens 1200 and max_options_per_question 32; [reduce_state/tree](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/questions.py#L279-L420); [plan](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/questions.py#L420-L481). | State estimate is not total wire token cap. Criteria and instructions also cost tokens. Generic trimming can remove decision-useful facts. Grouping can discard better alternatives before leaf choice; measure instead of assuming quality. |
| Hierarchical confidence | [SyncDecider._walk/_combine](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/provider.py#L322-L404) shares one deadline; partial joint distribution of entered leaf; minimum of available stage confidences. | Neither partial mass nor minimum confidence is calibrated end-to-end; missing stage confidence can be obscured. Do not normalize partial mass into full distribution or use it as unconditional probability. |
| Response validation | [parse_choice_response](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/addons/system_one/http_provider.py#L279-L345) checks chosen key, optional finite bounded probabilities, known keys/sum/chosen key, optional confidence, logs argmax mismatch; not strict full coverage or answer type in this function. | Distinguish legal selectable response from complete uncertainty data eligible for calibration/guarded authority. Test provider-specific contracts without breaking existing supported response shapes. |
| History and attribution | [history.py](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/history.py#L174-L239) bounded recent decisions; [loop.py: _decide](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/loop.py#L1485-L1750) emits systemone.requested before calling generic decider, checks actual result and logs actor attribution. | Generic request event is not an HTTP request counter; route veto and forced decisions must not be counted as model wins. Use stage attempts and accepted choice provenance. |
| Stale/transport | [loop.py](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/loop.py#L1485-L1630) discards stale frames, treats handoff timeout and terminal config error separately; [provider integration](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/provider-integration.md) describes deadlines, rate budget and breaker. | Retain hard response identity/config rejection, single decision budget and no stale execution. Measure outages and retries separately from normal latency. |
| Objectives | [ObjectiveSession](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/source.py#L131-L360) handles refresh, completion and optional Dex/Story fallback; [objective-file](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/objective-file.md). | Action mode support is not proof of autonomous strategic goal choice. Don't duplicate the session or bypass its revision/validation. Pure System One currently depends on available objective strategy/fallback; evaluate that limitation explicitly. |

Existing regression surfaces include [route veto](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_hybrid_route_step_veto.py), [adaptive gate](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_hybrid_adaptive_gate.py), [bounded handoff](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_hybrid_agent_handoff_timeout.py), [irreversible gate](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_hybrid_irreversible_escalation.py), [unknown scene](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_unknown_scene_escalation.py), [generic providers](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_system1_generic.py), [decision idle](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_realtime_decision_idle.py) and [frame token](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/test_step_choose_frame_id.py). These tests exist; not run or comprehensively assertion-audited in R03.

## 2. Proposed ownership model

Names are conceptual, not new required models or package names.

```text
      Goal source / operator / selected strategic policy
                        | validated objective + revision (CAS)
                        v
  Game adapter -> Perceiver -> immutable DecisionFrame
                        | typed legal options + factual consequences
                        v
             DecisionRouter (policy, no input authority)
              |        |          |           |
          observation  proven     System One  external agent
          settle       route      choice      choice / plan proposal
              |        |          |           |
              +--------+----------+-----------+
                        v
             Revalidate exact frame and option
                        v
                existing Executor / Actor
                        v
           verified outcome -> events / history / progress
                        |            |
                        |            +--> scoped recovery/replan
                        +--> next fresh frame
```

- **Runtime / System Zero (conceptual):** read exact supported state, derive options, choose nothing strategic, execute bounded known mechanics, verify and report. One writer. Game adapter owns generation mechanics, not provider prompt.
- **System One:** select one legal semantic option from compact state and consequences, including strategic/goal options in a future separately reviewed interface. Provider may be native choice or compatible chat, not tied to Jev/VON.
- **System Two / external agent:** plan and revise objectives, resolve ambiguity and complex strategy, inspect richer facts and request a new frame. No raw input or RAM access outside current controlled API. Only one active objective revision and one choice owner.
- **Policy/router:** decide authority and route based on evidence, explicit current intent, eligible risk and measured provider reliability; never create an executable option or override legality. No hidden extra agent/controller.

### Mode guarantees

| Mode | Primary choice owner | Goal authority | On provider/agent unavailability | Benchmark constraint |
|---|---|---|---|---|
| agent | external agent | operator/agent via existing objective API | current external wait/explicit stop policy; proposed bounded unattended profile requires review | no System One calls |
| system1 | configured choice model plus existing safe mechanical/forced behavior | existing objectives/explicit fallback today; proposed model-selectable validated goal intents in a future ADR | configured bounded retry/outage then safe pause; never silently call external agent | no agent calls or disguised agent interventions |
| hybrid | router admits deterministic subset, eligible System One, else external agent | same revisioned objectives; strategic agent handles complex plan changes | transient System One failure hands off where allowed; expired handoff safe pauses, terminal config rejection pauses | attribution per source; measure agent and model cost separately |

Pure System One must be a legitimate autonomous target, not just a hybrid fallback that needs a human at every ambiguous choice. A future **goal-choice** contract may let it select from adapter/strategy-generated, checkable goal candidates (progress/heal/restock/train/capture/team-prep/explore). This is a separate planning request, not a raw game action: validate target, success probes, applicability, explicit authorization, priority, budget, and revision before CAS into existing session. Do not change frozen DecisionFrame schema or add executor refs to planning. No arbitrary story completion, no invented locations, no overwriting a newer owner plan. If no feasible goal, pause/diagnose. Start with existing objective/ROM facts; scope a dedicated ADR before implementing and evaluate a pure-mode campaign without external intervention.

### Deterministic-before-provider gate (#132)

This is deliberately narrower than "pick top-ranked option". An action-bearing pre-call route choice is proposed only if all are proven: explicit active objective and unchanged revision, fresh supported ready scene, a current chooseable option with exact plan-step identity and bounded registered executor, proved target applicability and relevant postcondition, no risk/irreversible/conditional uncertainty, no disallowed goal constraint, no tactical or resource tradeoff hidden by the route, no alternate meaningful intent needed, no active recovery/handoff or override owner, no repeated no-effect route state. If evidence is absent, ambiguous or contradictory, ask the appropriate current owner or pause. In agent mode preserve existing explicit delegation/forced semantics; proposed deterministic-first authority is opt-in until reviewed.

```text
on ready frame:
    if invalid / unsupported / ownership uncertain: existing safe handling
    if approved non-decision settle: existing observation path
    if active bounded recovery / handoff: keep assigned owner
    if mode policy allows a uniquely proven pre-call route step: route_planner choice
    else:
        agent mode -> external choice
        pure system1 -> System One choice (never external)
        hybrid:
            if pre-gate scene/provider/intent ineligible: agent, no model request
            else request System One within its budget
            if malformed, stale, terminal config failure: existing typed handling
            if admitted candidate and policy allows: System One choice
            else: bounded agent handoff
    verify exact chosen frame and objective revision; revalidate; execute once
    observe verified outcome; update progress and routing history
```

High-risk/irreversible actions stay subject to existing explicit intent and hybrid irreversible gates; a single option, high confidence or route relevance does not authorize them. Do not change the pure-mode executor rule by routing to an agent; unavailable safe choice pauses. Never expand unknown scene primitives, permit confirmation, or bypass leak guard to get unstuck. Keep explicit strategy alternatives visible; deterministic equivalence (same authorized effect, no meaningful tradeoff) needs evidence, not an option name. Bound macro at warp/trainer/obstacle, battle, unexpected state, decision/revision change, input/frame/wall cap; return actual landing or typed interruption. One route-choice ID can be executed only once via current actor.

## 3. Decision quality before model upgrades

### Scene-specific decision profiles

Preserve existing `state/instructions/criteria` wire contract and leak guard. Proposed versioned profile per scene family, generated deterministically from facts:
- Battle: active/target identities, current HP/status/PP, relevant stats, move legality, damage range/KO chance/speed/accuracy where proven, status/ability/effect facts, objective (win/catch/survive/train), resource and switch consequences. Gen-specific and knowledge-policy-aware; unknown stays unknown.
- Field: current location and movement readiness, goal/revision context safe for provider (do not expose internal revision IDs if contract forbids), exact reachable candidate labels, route cost and prerequisites, interruption/failure context, recent useful changes, team/resource need facts. No raw edge/executor keys.
- Menus/dialog: scene/prompt evidence, relevant choices and effect facts, reversible/cancel distinction, no fabricated prompt interpretation; hand off unsupported/ambiguous cases using existing safe policy.
- Strategy: coarse goal candidates, feasible prerequisites, costs and explicit completion predicates, separate from action options. Intent persists until verified completion, explicit owner revision, material new evidence or bounded budget. Hysteresis for resource thresholds and anti-starvation is a proposed policy, not a safety gate or hardcoded walkthrough.

Required decision facts are protected from generic trimming. If profile cannot fit a provider budget with required facts, mark ineligible and route/pause; don't discard needed legality context. Record total estimated and actual usage for state + instructions + criteria, truncation, stage depth and option count. Optional richer agent briefing remains local; enforce visibility policy (fair_play vs guide separate cohorts). Validate fact/render changes by labeled case, not prompt length alone.

Keep the flat/hierarchical plan as baseline. Evaluate current 32-option limit vs focused facts and category semantics in offline/replay ablations; don't cap legal choices invisibly. A hierarchy must retain every eligible option exactly once; report selected branch and omitted mass, stage errors and full per-stage distributions. A missing confidence stage or truncated required facts makes that decision ineligible for probability-based admission. Partial joint mass must not be treated as fully calibrated. Compare fixed order vs deterministic shuffle only in isolated evaluation: historical [provider integration](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/provider-integration.md) reports first criterion chosen 81/102 in one run, which is not proof of positional bias without counterfactual matched frames. Test semantically identical permutations and record rank, selection changes, accuracy, ambiguity and token/call cost. Do not turn on randomized production choices.

### Provider and confidence policy (#134)

Separate three concepts: (1) legality and fresh execution, independent of model; (2) provider response validity, schema and identity; (3) demonstrated suitability for the scene/risk class. A returned scalar confidence is not probability of verified game progress; validate against labeled acceptable actions under fixed provider/model version, prompt schema, scene, option count, knowledge policy, game revision, flat/hierarchical, truncation and observation quality. Never fabricate confidence for chat or deterministic choices.

Proposed staged rollout per stratum, each explicitly approved:
1. **Shadow**: store model proposal only; existing authorized actor selects and executes. Evaluate labels and disagreements; counterfactual game outcome for non-executed proposal is unknown.
2. **Guarded**: admit only reviewed low-risk supported scene/provider strata with known identity, reliable payload, explicit policy and sufficient held-out evidence. For confidence-less chat, eligibility is empirical per stratum, not synthetic score. Missing/partial distribution can be a valid choice response but not a fully calibrated sample; unsupported/missing evidence routes agent in hybrid, or pauses in guarded pure mode.
3. **Expanded**: add independently accepted scene strata, not broad global threshold; re-evaluate when provider/weights, prompt profile, ROM/adapter or knowledge policy changes. Hold/rollback authority on evidence regressions, preserve data. Do not change existing defaults without maintainer approval.

Initial candidate evidence floor (proposed, not current requirement): 100 independent labeled scenario families per admitted stratum, at least 90% one-sided 95% Wilson lower confidence bound on acceptable-choice accuracy among admitted samples, plus report coverage/rejection and confidence reliability. Family-level independence or cluster bootstrap if correlated; never inflate n by replaying same save. For low-n or zero-coverage report insufficient. Separate rare/high-risk scenarios; no statistical score overrides legality or intent. Exact safety fixtures must pass with zero unauthorized inputs; native verification is separate. Do not claim calibration from 100 cases alone; plot reliability bins with counts and intervals, Brier/log loss only when label/distribution semantics fit (single-label full distribution or explicitly defined binary acceptability), and report coverage-risk curve. Choose thresholds on calibration data, lock on held-out test. Do not pool forced/local choices or repeated cached outcomes.

## 4. Recovery and liveness (#133)

Use existing no-progress/unknown-scene bounded windows, repeat gate, objective CAS, history and ledger. Distinguish:
- malformed/unavailable provider, terminal config/identity failure, stale observation (no input);
- blocked path, missing proven prerequisite, unknown scene, busy timeout;
- action executed but no intended effect; legitimate adverse outcome (miss, failed catch, no bite) not executor failure;
- map/warp cycles and objective oscillation, repeat encounter, repeated losses, exhausted resources.

Bound per-incident retry count and total action/decision/time budget; scope failure memory to objective revision, game identity, scene/state and relevant capability/effect (not entire old map forever). Changed position alone is not durable progress. For each intent use current RAM evidence (badge, validated arrival, item delta, owned species, objective-specific XP/EV/team improvement, successful required puzzle step). For repeated League losses, summarize lineup/levels/moves/resource and observed cause; propose a new training/team/restock objective rather than blindly repeat or hardcode five levels. Existing recovery window only rearms with verified durable progress; unknown-scene primitive limits unchanged. No automatic checkpoint rollback: any restore needs separate explicit owner policy, exact state/identity and progress-loss receipt. Pure System One uses same typed diagnostics and a bounded legal goal revision proposal when supported; never forced external fallback.

## 5. Provenance and evaluation contract

Do not silently redefine existing events. Propose additive, reviewed provenance fields/event projection for one logical decision, with distinct action/choice IDs, exact frame and objective linkage kept private; reuse existing EventEnvelope, calibration JSONL and ledger. Candidate fields:
- decision origin: observation / forced-local / route_planner / system1 / agent; separate attempted provider and final authority, rejected/vetoed/overridden choice and reason, ownership epoch/handoff state. A route pre-call result emits no fake model completion; preserve compatibility for consumers and update schema/readers/tests together.
- provider call ledger: logical request, stage, actual HTTP attempts (including retries/timeouts), latency end-to-end and per attempt, reported model/version, protocol, option count/order/schema, truncation, billed/estimated tokens and usage missingness, rate/breaker/outage wait. Generic requested events are not billable calls. Model inference cost from actual usage and versioned prices; estimates vs billing receipts separate.
- effect: validated accepted option vs applied/verified/rejected/interrupted/timed_out/uncertain, game outcome and durable progress as separate fields; executed/observed/verified milestones counted once, no false success on position or confirmation alone.

**Metrics** (report per mode, scenario stratum and run, with denominators):
- accepted-choice quality = acceptable labeled model proposals / all labeled valid model proposals; separately report invalid, timeout and unknown rates over all logical attempts; guarded accuracy denominator is admitted labeled proposals, with admission coverage (admitted / all eligible offers).
- safety: unauthorized input count, stale and illegal attempted inputs; safety fixture requirement zero.
- effectiveness: verified intended outcomes / executed action-bearing choices; legitimate failed game outcomes separate; durable objective milestones per game-hour and per logical action-bearing decision. Do not use this as per-turn correctness without labels.
- efficiency: actual HTTP attempts per decision, logical requests per verified milestone, input/output tokens, costs per verified milestone and successful campaign; also include failed/censored runs and agent costs. Deterministic share = proven local/route action-bearing decisions / all action-bearing decisions. Observation waits separately.
- responsiveness: p50/p95 provider and end-to-end decision latency, handoff wait, outages; progress per wall/game hour; counts of safe pauses, stuck episodes, wipes, recovery success and lost checkpoints. Report missing usage/identity and coverage, not zero-filled.
- campaign: completed unattended / all started seeds, exact RAM Hall of Fame proof and required badges/gates; separate postgame/all-mechanics. #135/#138 propose >=90% main-story completion, not achieved or changed here. Record CI/native runtime evidence and distinct abort/failure/censoring; no dropping failures.

Evaluation layers:
- L0 public ROM-free contract fixtures and mock faults: no credentials, ROM or game data. Confirm current tests and add focused cases per PR.
- L1 offline replay of reviewed synthetic or private sanitized decision frames: matched labels, provider identity and versions, prompt/order/hierarchy ablations. Do not execute actions during replay; label from independent game rules/human review, multi-acceptable/unknown explicit.
- L2 battle simulator (R06) and private native scenarios: separate result types; compare exact ROM, verified initial state hash/seed/backend, rules, objective and observation policy. Prepared/RAM-written fixtures explicitly marked, not campaign.
- L3 trusted native multi-seed campaigns (R09/#135/#138): fixed starting ROM/revision/host/backend, agent/prompt/config/model pins, same budget and outcome gates, same observation-policy cohort; independently repeat agent, pure System One and hybrid. Record operator interventions as non-autonomous; no checkpoint replay disguised as new seed. Compare paired seeds where possible; avoid claiming causality from unpaired stream anecdotes.

See [evaluation case manifest](../research/system-one/system-one-evaluation.json), proposed cases, not executed. Train/calibration/test separation by run/save/near-duplicate scenario family and map context; freeze holdout before tuning. Keep private data outside public repository; no raw screenshots, game text tables, secrets, raw executor args or private state files. All additions require review and PR-head CI.

## 6. Implementation sequencing (proposed, not assignments)

| Slice | Existing issue | Deliverable / gate |
|---|---|---|
| R03-A | [#132](https://github.com/aimd-labs/pokegent/issues/132) | characterize actual paths incl singleton, veto and event attribution. Add pre-call proof in shadow mode; test exact route and negative controls. No new input authority. |
| R03-B | [#132](https://github.com/aimd-labs/pokegent/issues/132), [#133](https://github.com/aimd-labs/pokegent/issues/133) | admit reviewed deterministic subset with event provenance; ensure zero extra model calls for eligible route, no skipping meaningful alternatives, preserve recovery/handoff. |
| R03-C | [#134](https://github.com/aimd-labs/pokegent/issues/134) | versioned scene profile, payload sufficiency and provider uncertainty/identity receipt; flat/grouped and order ablations. Reconcile obsolete indefinite-wait and confidence docs. |
| R03-D | [#134](https://github.com/aimd-labs/pokegent/issues/134), R09 | offline eval harness and labeled grouped split; shadow -> guarded -> expanded only per measured eligibility. No invented confidence or default-threshold change. |
| R03-E | [#133](https://github.com/aimd-labs/pokegent/issues/133), [#135](https://github.com/aimd-labs/pokegent/issues/135), [#139](https://github.com/aimd-labs/pokegent/issues/139) | objective/intent proposal ADR, existing CAS and bounded recovery; pure system1 no-agent goal-choice proof before broader rollout. |
| R03-F | [#135](https://github.com/aimd-labs/pokegent/issues/135), [#138](https://github.com/aimd-labs/pokegent/issues/138) | native scenarios and matched multi-seed campaigns by mode, proof and cost. |

Operational P0 incidents remain prioritized per R02; do not duplicate #165, #178/#179, #180, or restart paused #106. No issues created/closed or labels changed. No provider migration, live service, runtime implementation or benchmark execution authorized by this research. Current docs and contracts are not replaced by this proposal.
