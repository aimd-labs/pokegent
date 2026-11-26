# System One endpoints and model switching

System One is an optional choice provider. `primary: http` and the custom `systemone`
protocol remain the defaults. The explicit `openai-chat` adapter translates the same safe
state, instructions and criteria into nonstreaming chat messages, using the shared HTTP
transport. Setting a URL alone does not translate protocols. There is no autodetection,
provider fallback, native Ollama adapter, built-in host or model selector.

User YAML uses `system1`; the legacy section uses `system_one`. File settings precede
nonempty environment overrides, then explicit CLI settings. Legacy YAML supports
`${NAME}` and `${NAME:-default}` expansion. Config errors stop startup before gameplay.

| User key | Environment | Default and contract |
|---|---|---|
| `base_url` | `SYSTEM1_BASE_URL` | None; required for enabled HTTP. HTTP(S), host and valid port; no credentials, query, fragment or controls. Legacy: `provider_url`. |
| `protocol` | `SYSTEM1_PROTOCOL` | `systemone` or explicit `openai-chat`; no other protocols. |
| `endpoint_path` | — | `/v1/systemone` for custom, `/v1/chat/completions` for chat. Advanced absolute-path override, without query, fragment or controls. |
| `request_model` | `SYSTEM1_REQUEST_MODEL` | Unset. Explicit selector, at most 200 characters without whitespace. Unset/empty omits custom-wire `model`; chat requires a selector. |
| `model` | `SYSTEM1_MODEL_ID` | Unset. Optional expected **response** identity, never a selector. `SYSTEM1_MODEL` is an alias; nonempty `SYSTEM1_MODEL_ID` wins. |
| `strict_model_pin` | — | `false`. When true, requires a configured `model` and an actually reported matching identity; missing identity is terminal too. |
| `api_key` / `api_key_env` | `SYSTEM1_API_KEY` | Unset; no auth header. Prefer `api_key_env` with the secret in the environment. Legacy uses `api_key` with env expansion. Values must be printable ASCII without controls. All accepted keys, including short ones, are scrubbed. |
| `auth_header` | `SYSTEM1_AUTH_HEADER` | `Authorization` (case insensitive), sending `Bearer <key>`. Another valid HTTP header token sends the raw key. Reserved transport headers and injection are rejected. No arbitrary header map. |
| `timeouts` | `SYSTEM1_TIMEOUT` | 8 seconds. Scalar or `{request: 8}`; finite numeric `0 < value <= 120`, never boolean. Legacy: `timeout_seconds`. |
| `connect_timeout_seconds` | — | 2 seconds, capped by remaining deadline. |
| `read_timeout_seconds` | — | Half the decision budget, capped by remaining deadline. Socket inactivity limits supplement the total deadline. |

Prefixes are preserved with matching path components overlapped: base `http://localhost:8000/v1`
plus chat's default path resolves to `/v1/chat/completions`; `/gateway/v1` resolves to
`/gateway/v1/chat/completions`. A base already ending in the complete endpoint is used once.
Explicit `endpoint_path` follows the same joining rule.

Only one exact criterion ID is accepted from chat: `choices` must contain one entry with
`message.role: assistant`, string `message.content` containing the ID (surrounding whitespace
is allowed), and `finish_reason: stop`. Tool/function calls, partial responses, extra prose,
unknown IDs and malformed output are rejected. No vendor-specific structured-output extension
is required. Optional response `model` is recorded as reported; absent identity stays unknown.
Chat confidence and probabilities stay absent because this interface supplies no calibration.

Default hybrid escalates missing confidence to its agent and can wait indefinitely, including
when its threshold is zero. The autonomous chat examples below explicitly select `system1`.
Frame legality, fresh-state revalidation and irreversible-action safeguards still apply;
changing a model or threshold grants no new input authority.

Local compatible chat example (private run config; replace the selector):

```yaml
decider: system1
system1:
  protocol: openai-chat
  base_url: http://127.0.0.1:8000/v1
  request_model: <installed-model-id>
  timeouts: 8
```

Local Ollama-compatible example, **only through its enabled OpenAI-compatible chat interface**:

```yaml
decider: system1
system1:
  protocol: openai-chat
  base_url: http://127.0.0.1:11434
  request_model: <installed-model-id>
  timeouts: {request: 8}
```

Verify that the chosen server/version supports the response shape above. Native `/api/chat`
is not implemented here. An external bridge can instead implement the custom `/v1/systemone`
contract; raw native/chat servers do not accept the custom body. Configure authentication
for either local example if the chosen server requires it.

Hosted compatible chat example:

```yaml
decider: system1
system1:
  protocol: openai-chat
  base_url: https://provider.example
  request_model: <provider-model-id>
  api_key_env: SYSTEM1_API_KEY
  auth_header: Authorization
  timeouts: 8
```

Use a provider-documented custom header such as `X-API-Key` only when required. For a hosted
custom System One server select `protocol: systemone` and its documented path/selector contract.
Keep real hosts, keys, model selections, run configs and game data outside this checkout.
See [historical custom upstream evidence](provider-integration.md#historical-upstream-facts-wfzyxvon-pinned-in-source-lockmd)
for the pinned upstream contract, which did not accept a request selector.

With explicit authority to call the candidate endpoint, use the existing probe:

```sh
pokegent provider check --config /path/to/private/run.yaml --live
```

This makes an informational unauthenticated health request and **one logical model decision**;
configured transient retries can make multiple HTTP attempts. Health has a total budget of
`min(3, timeout_seconds)` seconds and the decision has its own `timeout_seconds` total budget.
Both budgets include DNS, connection opening and reads. Hostnames use a short-lived resolver
process that is killed and reaped on expiry or cancellation; no resolver thread is abandoned.
Expiry interrupts active sockets, including peers dribbling chunked bytes, and the reader owns
response cleanup. A health/config
success alone does not prove inference. Safe config summaries show key presence only;
check the actual reported response identity in the probe result, not just the configured pin.
Do not paste credentials into commands, logs or reports.

Explicit request/auth configuration rejections (HTTP 400/401/403/404/405/415/422) and failed
strict pins stop for `owner_review` with stable `provider_configuration` details. Error reasons
come only from recognized JSON message/detail/error fields, are bounded, scrubbed and stripped
of controls. The runner stops before input and follows its normal validated final-save path.
Supervisor publishes a blocked lifecycle and `exit.json`; a valid successor never overrides
this block, including recovery after summary/exit/lifecycle publication. Other invalid outputs,
ordinary paused segments and transient 408/429/5xx retain their existing handling.

An owner-requested model switch follows a controlled single-writer boundary:

1. Stage a candidate private config and retain the reviewed prior config for rollback. Select
   the correct protocol and request model separately from any optional true response pin.
2. Under explicit authority, probe the candidate and verify its **actual response `model`**.
   If it is missing, identity remains unverified; strict pinning must fail. Review compatibility
   and latency before use. A synthetic test pass does not establish live acceptance.
3. Have the maintainer apply the reviewed config at a safe segment boundary. Environment
   edits are not hot reload. Preserve the accepted state pointer, state files, objective and
   objective history. Confirm the next segment's manifest, including actual response identity.
4. If necessary, roll back config at the same boundary. A persisted rejection block requires
   explicit owner reconciliation; do not delete history, promote a successor blindly or keep
   restarting the supervisor. Routine review does not authorize service restarts or changes
   to the owner agent session's model settings.

## Labeled choice evaluation (default off)

PR #267 adds a local evaluation API in `addons/system_one/evaluation.py` for real
`DecisionFrame`/`ChoiceResult` pairs. It is not connected to the runner or a config flag,
makes no provider call, and grants no authority. `evaluate_labeled_choices(cases)` is inert
unless called with `enabled=True`.

Each `LabeledChoiceCase` pairs a frame, an optional result, acceptable option IDs and a
`family_id`. Labels must be a nonempty `frozenset` of visible option IDs; `None` means the
label is unknown. Unknown-label cases stay out of label denominators but remain in
`supplied`. A result is valid only when its `decision_id` matches the frame and
`check_choice` accepts the option against the visible set, so a missing or hidden option is
invalid. A duplicate `(run_id, decision_id, provider, model)` case is rejected. Input is
capped at 1000 cases, 256 label IDs and 128 characters per identity string. A
no-prediction row has unknown provider and model identity.

The two rates answer different questions. `acceptable_choice_rate` divides acceptable valid
labeled choices by labeled valid choices, a conditional accuracy over supplied labels.
`labeled_case_success_rate` divides by every labeled case, so invalid choices and no
predictions remain in its denominator. Family counts count distinct supplied or labeled
`family_id` values; they are not independent trials. Labels and family grouping are caller
supplied and not certified, and the report is not a safety proof. Eligibility is
`not_evaluated`, `authority_granted` is false, and no Wilson interval, threshold or stage
promotion follows from the result.

This local API is separate from `hybrid.shadow`, which makes one observational provider call
during a run ([configuration](configuration.md)), and from the `frame_choice_validity`
logging switch, which only records `check_choice`'s verdict in
[provider.py](../addons/system_one/provider.py). It does not change default, shadow or hybrid
behavior and does not claim guarded or expanded implementation. Tests live in
[test_labeled_choice_evaluation.py](../tests/test_labeled_choice_evaluation.py).

Example, using synthetic IDs with two visible legal options, four distinct decision IDs in
one supplied family and labels `{tap:a}`. This is a synthetic sanity case, not independent
calibration data:

```python
from addons.system_one.evaluation import LabeledChoiceCase, evaluate_labeled_choices
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option

OPTIONS = (
    Option(id="tap:a", label="Legal A", legality=Legality.LEGAL, executor_ref="test.action"),
    Option(id="tap:b", label="Legal B", legality=Legality.LEGAL, executor_ref="test.action"),
)


def frame(decision_id):
    return DecisionFrame(
        decision_id=decision_id, run_id="run-synthetic", snapshot_seq=0,
        scene_id="field.synthetic", scene_confidence=1.0,
        state_fingerprint="sha256:" + "0" * 64, options=OPTIONS,
    )


def case(decision_id, option_id):
    result = None if option_id is None else ChoiceResult(
        option_id=option_id, decision_id=decision_id,
        provider_id="synthetic-provider", model_id="synthetic-model",
    )
    return LabeledChoiceCase(frame(decision_id), result, frozenset({"tap:a"}), "family-synthetic")


cases = (case("dec-a", "tap:a"), case("dec-b", "tap:b"),
         case("dec-c", "tap:missing"), case("dec-d", None))
disabled = evaluate_labeled_choices(cases)
assert disabled["enabled"] is False and disabled["evaluated"] is False
report = evaluate_labeled_choices(cases, enabled=True)
assert report["counts"] == {
    "supplied": 4, "labeled": 4, "unknown_label": 0, "no_prediction": 1,
    "invalid_choice": 1, "valid_choice": 2, "labeled_valid_choice": 2,
    "acceptable_choice": 1,
}
assert report["rates"]["acceptable_choice_rate"] == 0.5
assert report["rates"]["labeled_case_success_rate"] == 0.25
assert report["supplied_family_count"] == 1
assert report["labeled_family_count"] == 1
assert report["eligibility"] == "not_evaluated" and not report["authority_granted"]
```
