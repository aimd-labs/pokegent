# System One provider integration

See [System One endpoints and model switching](system-one.md) for the supported custom/chat
protocols, generic configuration, local/hosted examples and terminal-rejection lifecycle.

Code: `addons/system_one/`. Spec: chapter 8 (interface, questions, calibration), 12.3 (error
classes), 14 (config), 16.3 (measurements). Contract: `docs/contracts.md`.

## Historical upstream facts (wfzyx/von, pinned in `source-lock.md`)

Read at `56e845299f573af06480db6035f5d13f0f3baf51` (README.md, `src/von/types.py`), Apache-2.0,
own client, no code copied. These are historical contract evidence for that revision,
not a universal contract for every compatible endpoint.

- `POST {base}/v1/systemone`, JSON:
  `{"state": <any JSON or text>, "questions": {"<name>": {"type": "choice",
  "instructions": "...", "criteria": {"<key>": "<description or null>"}}}}`.
- That historical server's wire contract accepts only `state`,
  `questions`, `request_id` and `lane` (`extra="forbid"`); a `model` field is an HTTP 422. The model is
  fixed server side (weights `wfzyx/von @ 5df8185a`); it reports itself as `von-1.3.0` (the SDK does not
  count patch versions) in the answer as `model` / `model.model`. `GET /health` answers
  `von_version` and `weights_revision`. Leave `request_model` unset for that contract.
  The client reads a string or an object response model and compares against the expected
  response id when one is configured.
- Response: `{"model", "answers": {"<name>": {"type": "choice", "choice", "probabilities",
  "confidence"}}, "usage": {"input_tokens", "output_tokens"}, "truncation"?}`.
- Auth: optional `Authorization: Bearer <SYSTEM1_API_KEY>` (server enables it with `SYSTEM1_API_KEY`).
- Truncation: a state longer than the server's `VON_MAX_STATE_TOKENS` (default 8192) is
  middle-truncated (60 % head, 40 % tail) and reported by `X-Von-Truncated`, `Warning` and the
  `truncation` field; with `--on-overflow refuse` the server answers HTTP 422.
- English only. `Choice.criteria` values may be any JSON; we send plain English strings.
- Not verified against a live server in M3.0 (see "Not verified").

## Configuration

`system_one` section of the spec-14 config (extra keys marked *):

```yaml
system_one:
  enabled: true
  primary: http                    # http | mock
  protocol: systemone              # default; openai-chat requires request_model
  auth_header: Authorization       # Bearer; other allowed names carry raw key
  provider_url: ${SYSTEM1_BASE_URL}   # http(s) base URL; /v1/systemone is appended
  endpoint_path: /v1/systemone  # optional path override
  model: ${SYSTEM1_MODEL_ID:-}        # optional: the id the server is EXPECTED to report; never sent
  request_model: ${SYSTEM1_REQUEST_MODEL:-} # sent as `model` only when set
  api_key: ${SYSTEM1_API_KEY}         # optional; SecretStr, never printed
  timeout_seconds: 8              # hard budget of ONE decision (all stages, all retries)
  connect_timeout_seconds: 2      # *
  read_timeout_seconds: null      # * per attempt; default timeout_seconds / 2
  fallback_provider: null         # only null for now
  max_state_tokens: 1200
  max_options_per_question: 32
  option_order: frame             # * experiment switch: frame | shuffle (see "Option order experiment")
  require_fresh_snapshot: true    # enforced by the executor's revalidation; recorded here
  strict_model_pin: false         # * true: requires the configured pin and matching reported identity
  retry:    {max_attempts: 3, backoff_base_seconds: 0.25, backoff_max_seconds: 4.0, jitter: 0.5}  # *
  breaker:  {failure_threshold: 5, open_seconds: 30, half_open_max_calls: 1}                      # *
  rate:     {max_requests: 120, per_seconds: 60, burst: null}                                     # *
  calibration:
    mode: observe_first
    confidence_gate: disabled_until_firered_validated   # the only accepted value
    log_path: runs/<run-id>/system_one_calibration.jsonl  # *; null = default <out>/calibration.jsonl
```

`calibration.log_path` and `<run-id>`. The literal `<run-id>` stands for "this run":
`pokegent run` resolves it (`policy.resolve_log_path`) so the log lands INSIDE the run directory
(`--out`) as `<out>/<file name of log_path>`; the directory part of the configured path is
replaced by the run's own, and a `<run-id>` in the file name itself expands to the run id (letters,
digits, `.`, `_`, `-`). Without a run directory the placeholder expands to the run id. If neither is
known (a library caller, a path that names no file) it is an error (`AppError` / `ValueError`),
never a directory literally named `<run-id>` (the first real run, 2026-10-08, created one in the
cwd). A path without the placeholder is used as written; `null` writes `<out>/calibration.jsonl`
under `pokegent run`, and no file elsewhere. `addons.system_one.factory.build_decider`
refuses a path that still contains the placeholder.

Environment: `SYSTEM1_API_KEY` may be unset (warning, no Authorization header);
`SYSTEM1_BASE_URL` is required when System One is enabled; `SYSTEM1_MODEL_ID` is an optional
expected response ID, while `SYSTEM1_REQUEST_MODEL` is a separate request selector
(`${NAME}` and `${NAME:-default}` expand in every string). Expired legacy names fail with a migration error; use
this release. Unknown keys, bad ranges and a non-http(s) URL are errors, all listed
at once with their path; rejected values are never echoed. Plain `repr`/`str`/`model_dump` of the
config show `SecretStr('**********')`.

Set `provider_url`, `endpoint_path`, `request_model` and `api_key` for an endpoint you are
authorized to use. Confirm its supported model IDs and wire format with the provider.
The client omits JSON `model` when `request_model` is unset; it sends the explicit value
when set. Endpoint contracts vary: some select a model server side or reject this field,
while others require it. Configure a supported selector explicitly for the latter;
there is no built-in endpoint or model default. `model` / `SYSTEM1_MODEL_ID` remains
the expected response ID for telemetry and `strict_model_pin`, never a selector fallback.
For user-facing YAML use `system1.request_model`; `SYSTEM1_REQUEST_MODEL` overrides
it when non-empty. The environment-only `provider check` and app fallback use the same
variable. Explicit legacy `system_one` YAML uses the expansion shown above.

## Wiring

```python
from addons.system_one import load_config
from addons.system_one.factory import build_decider
cfg = load_config("config.yaml").config           # LoadedConfig(.config, .warnings)
decider = build_decider(cfg, calibration_path=run_dir / "system_one_calibration.jsonl")
runner = Runner(decider=decider, ...)             # SyncDecider is a frozen-contract Decider
```

`SyncDecider.decide(frame)`: leak guard (`PayloadLeakError`, `E_PAYLOAD_LEAK`) -> question plan
-> provider call(s) under one deadline -> `ChoiceResult` with `decision_id` echoed. A frame with
exactly one option is answered locally (no request). Manifest per run: `HttpProvider.manifest()`
(`provider_version`, `server_version`, `model_id`, `response_model`, `prompt_schema_version`,
`upstream_sha`); `SyncDecider.manifest()` adds `option_order` and `pokegent run` writes it to
`summary.json.decider_manifest`.

## Questions

One `Choice` per decision (`questions.action`): `criteria = {option_id: text}` exactly as
`to_system_one_payload` built them; English instructions; `state` = scene, goal, prompt and the
frame context. Over `max_state_tokens` (deterministic estimate, see `questions.estimate_tokens`;
the real count comes back as `usage_in`) the context shrinks in this order and the result carries
`truncated=True`: trim lists to 5, trim lists to 2 (history-like keys keep the newest items),
drop optional keys (history-like first, then largest), shorten `prompt`/`goal`. `scene` is never
touched. More than `max_options_per_question` options: stage 1 `choose_category`, stage 2
`choose_target_in_category`. Categories = the option's `verb` (else the id prefix); a category
over the limit is split into ordered `<name>_part_<n>`; more categories than the limit are paged.
Descriptions only count and name options ("5 options, such as ..."); every option is in exactly
one category. The tree is in the calibration record. `ChoiceResult.probabilities` of a
hierarchical decision is the chosen category's options scaled by the category probability
(partial by design); confidence is the minimum over stages. Both are logged only.

## Option order experiment (`system_one.option_order`)

**An experiment switch for positional-bias analysis. Never use `shuffle` in production play.** The first real run
(m4-run1) picked the first criterion in 81 of 102 decisions, and the battle family emits item options first; to find out
how much of that is position, `option_order` changes ONLY the order in which the criteria are sent:

| Value | Order of `criteria` in the request |
|---|---|
| `frame` (default) | the frame's option order, exactly as before the switch (a regression test pins the request body byte for byte) |
| `shuffle` | a deterministic permutation: the options sorted by `sha256(run_id, decision_id, stage, option_id)`; same decision, same order, in any process, no global RNG, no clock |

Everything else is the same in both modes: option ids, criterion texts, the option set, `option_set_hash`,
legality, the answer-to-option mapping (by option id, validated against the original frame), the state and the
instructions. A frame with one option is still answered locally (no request, nothing to order).

Hierarchical plans (more than `max_options_per_question` options): the tree is built from the frame order as always
(categories by `verb`, parts, pages, the sample labels in category descriptions). `shuffle` then permutes the keys
inside EACH question separately (the stage and its category key are part of the seed): the category question lists the
categories in a shuffled order, the target question lists the chosen category's options in a shuffled order. Nothing
moves across categories and the audit tree (`plan.tree`) is the frame's tree in both modes.

Where it is recorded (additive keys): every calibration row has `option_order` and `sent_order` (the option ids in the
order the last question was sent, the leaf question where option ids are sent; the planned order if no request left),
`plan.option_order`, and each `stages[].criteria_keys` is the order of that stage as sent;
`SyncDecider.manifest()` carries `option_order` and `pokegent run` writes it to `summary.json.decider_manifest`.
`load_config` warns when the value is not `frame`, so `pokegent run` prints and records it. For the analysis,
compare the rank in `sent_order` of the chosen option with its frame position over a `shuffle` run; with `shuffle`
the chance of any option being first is uniform by construction.

## Error handling (all inside one `timeout_seconds` budget)

| Situation | Provider error | Frozen code | Retried by client | Runner reaction |
|---|---|---|---|---|
| timeout, 408 | `ProviderTimeout` | `E_DECIDER_TIMEOUT` | yes, bounded | `decision.systemone.timeout`, one runner retry, escalate |
| connection error, 429, 5xx | `ProviderUnavailable` | `E_DECIDER_FAILED` | yes, bounded, `Retry-After` honored | `...invalid`, escalate |
| 4xx (also 401, 422) | `ProviderRejected` | `E_DECIDER_FAILED` | never | escalate |
| key outside criteria | `InvalidResponse` | `E_CHOICE_NOT_IN_FRAME` | never | escalate, no input |
| NaN/inf, bad sum, bad confidence, bad JSON | `InvalidResponse` | `E_CHOICE_MALFORMED` | never | escalate, no input |
| breaker open | `CircuitOpen` | `E_DECIDER_FAILED` | no request made | escalate |
| request budget used up | `RateBudgetExceeded` | `E_DECIDER_FAILED` | no request made | escalate |
| deadline hit (any stage) | `DeciderTimeout` | `E_DECIDER_TIMEOUT` | - | as timeout |

Breaker: closed -> open after `failure_threshold` consecutive transient failures (4xx and invalid
answers do not count), open -> half-open after `open_seconds`, one probe closes or re-opens.
Validation: answer must be a criteria key; probabilities finite in [0, 1], keys within the
criteria, sum within 0.02 of 1; confidence finite in [0, 1]; `NaN`/`Infinity` literals refused.
Server truncation (header or field) sets `truncated`. No fallback provider and no default
answer exist: with System One down the runner escalates and presses nothing (spec 8.4).
In hybrid mode, that escalation is handed to the loopback agent and waits indefinitely for
`/choose`; it never applies System One's suggestion because the agent is slow.

## Telemetry

The runner emits `decision.systemone.{requested,completed,timeout,invalid}` around every
`decide`. For use outside the runner (tools) pass an event sink and
`emit_events=True` to `build_decider`; do not do both. Event details avoid the key `token`
because the event log masks such keys (`usage_in`/`usage_out`). Telemetry is fail-open: a failed
write logs a warning and never changes the decision.

Calibration log: JSONL, append-only, one line per decision attempt, no wall clock (`measured_ms`,
also per stage, is the only non-replayable value), sorted keys. Fields: `schema`, `record_seq`,
`decision_id`, `run_id`, `snapshot_seq`, `scene_id`, `goal_id`, `option_set_hash`, `option_count`,
`state` (as sent), `instructions`, `criteria` (all), `option_order`, `sent_order`, `plan` (hierarchy tree, reduction steps,
estimates, `option_order`), `stages[]` (`name`, `request_id`, `path`, `criteria_keys`, `choice`, `probabilities`,
`confidence`, `truncated`, `model_id`, `raw_response_ref`, `attempts`, `usage_in`, `usage_out`,
`truncation`, `response_model`, `model_mismatch`, `argmax_mismatch`, `server_version`, `breaker`,
`measured_ms`), `choice`, `probabilities`, `confidence`, `model_id`, `provider_id`,
`provider_version`, `prompt_schema_version`, `truncated`, `state_reduced`, `server_truncated`,
`usage_in`, `usage_out`, `attempts`, `gate` (`disabled`), `outcome` (`ok|timeout|invalid|error`),
`error_code`, `error`, `measured_ms`. Observed outcomes and gold labels are joined later by
`decision_id`; split train/test by run/map/save state, never within one family (spec 16.3).
No secrets, executor references or argument values are written.

## Stub server and checks

```bash
. .venv/bin/activate
ruff check .
python -m compileall pokegent addons
python tests/games/gen3/test_imports.py
pokegent provider check                       # env config + mock and stub self-tests
pokegent provider check --config config.yaml
pokegent provider check --config config.yaml --live   # ONE real request to the configured provider
```

The stub (stdlib, 127.0.0.1, ports 19700-19799 only) answers a valid Choice, a wrong key, NaN or
Infinity probabilities, a bad sum or confidence, broken JSON, slow responses, 4xx/429/5xx bursts
and truncation headers, scripted per request (`server.script(valid(), status(503), ...)`).

## Not verified (M3.0)

Everything production-specific: the configured endpoint path, the model id the server
reports in its answers, auth, real latency (p50/p95), whether the server returns `truncation` as documented, and
how SYSTEM1 ranks FireRed options. The default 8 s timeout and the retry/breaker numbers are
conservative starting points to be tuned against measured p95 (spec 14).
# User-facing naming

System One is the generic user-facing decider name: use `--decider system1`.
The provider id is the neutral `http` (`system_one.primary: http`). The HTTP implementation
may retain `system1` internally for protocol compatibility. Configuration uses
`SYSTEM1_BASE_URL`, `SYSTEM1_API_KEY`, `SYSTEM1_REQUEST_MODEL`, and
`SYSTEM1_MODEL_ID` (expected response ID; `SYSTEM1_MODEL` also maps to this in user config).
The endpoint `/v1/systemone` and `X-Von-*`
wire headers are unchanged protocol names.

