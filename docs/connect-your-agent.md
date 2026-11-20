# Connect your own agent or model

Use Python 3.11 and the installed checkout environment. Emulator runs also require
[local ROM data setup](local-data.md#explicit-local-setup) and
[native mGBA prerequisites](native-install.md). Keep your ROM, saved state, generated
files and run configuration outside the checkout. Start a fresh game with the
[clean-start recipe](clean-start.md), which records the accepted local result and host limits.

## Agent mode: inspect, choose, verify

Start from your own validated saved state, with a fresh private output directory:

```sh
pokegent run --rom "$FIRERED_ROM_PATH" --state /absolute/private/start.state \
  --decider agent --external-port 19760 --max-steps 5 --out /absolute/private/agent-demo
```

The bridge defaults to `http://127.0.0.1:19760`; `--external-port` selects a port in
19700–19799. Use that same port for every request. It is unauthenticated and binds
only to loopback; keep it local. It is separate from the dashboard on port 19701.

From this checkout, in another shell using the same environment:

```sh
python -m examples.choose_inspected_frame --port 19760
```

The example reads `GET /briefing` for planning facts, then `GET /frame?wait=0` for
a pending decision. Read the frame's state, instructions, option criteria and any
feedback before entering an offered `options[].id`. Empty input leaves it pending.
Your own agent can replace the human selection step with its policy.

The example uses the existing `pokegent.server.request` client (35-second HTTP
timeout) and sends exactly the token it inspected:

```json
{"frame_id": "<inspected-frame-token>", "option_id": "<id-from-that-frame>"}
```

`POST /choose` returns HTTP 202 with `accepted: true` and `status: queued`; that
acknowledges delivery, not successful game input. Inspect the next frame's `feedback`
and the run's `events.jsonl` for execution and RAM verification. Repeat inspection
for each decision. `/status` reports `waiting_for_choice` while the runner waits;
a no-frame response is a status object, not an option set. Agent waits are indefinite.
`/frame` normally long-polls for up to 30 seconds; `?wait=0` avoids that wait.

The server refuses stale frame tokens with HTTP 409 and malformed or unknown option
choices with HTTP 400. The example also refuses IDs absent from its inspected set.
On a stale token, inspect again and reconsider the choice; never copy an old choice
onto a new token automatically. Request failures stop the example without retrying.
`pokegent step choose <option-id> --frame-id <inspected-token>` submits that exact
frame id without refetching, so a stale choice cannot rebind to a later frame;
with `--frame-id`, the CLI prints the `/choose` acceptance response, which confirms
the choice is queued but does not verify a game action. Without `--frame-id`, it
still refetches the current frame, binds to it, and prints the subsequent fetched frame.

Offered actions come from RAM-backed legality checks. Before input, the executor
rechecks the scene, state fingerprint and action against fresh RAM, then verifies
the outcome. Unsafe or unknown legality fails closed. Some unknown scenes in hybrid
mode have a bounded conservative primitive window; it does not grant arbitrary input.
See [runtime contracts](contracts.md) and [bridge details](server.md).

## OpenAI-compatible System One model

Create a private YAML file with this user configuration, replacing the endpoint
and model placeholders with values supported by your chosen server:

```yaml
decider: system1
system1:
  protocol: openai-chat
  base_url: http://127.0.0.1:8000/v1
  request_model: <supported-model-id>
  api_key_env: SYSTEM1_API_KEY
  auth_header: Authorization
  timeouts: {request: 8}
```

Keep the key in `SYSTEM1_API_KEY` in your environment, supplied through your own
secret handling. Omit `api_key_env` for a local endpoint that needs no key.
`Authorization` sends `Bearer <key>`. `request_model` selects the model;
`system1.model` is only an optional expected response identity. Environment
`SYSTEM1_*` settings override the file, so review them before use.

The adapter sends a nonstreaming `POST /v1/chat/completions`, preserving base-path
prefixes without duplicating `/v1`. It supplies a system instruction and a user
message containing state and criteria. A compatible response has exactly one choice:

```json
{"model":"reported-model-id","choices":[{"message":{"role":"assistant","content":"<exact-criterion-id>"},"finish_reason":"stop"}]}
```

Only an offered criterion ID is accepted (surrounding whitespace is allowed).
Malformed JSON, extra prose, unknown IDs, tool calls and incomplete responses are
rejected. Compatibility depends on the server and model; an OpenAI-compatible label
alone does not prove it. Chat supplies no calibrated confidence, so default `hybrid`
escalates these choices to its agent; this configuration selects `system1` explicitly.

The request budget is eight seconds here, including transient retries. Connection
and read limits also apply; malformed replies are not retried. Explicit configuration
HTTP rejections stop for owner review. See [provider bounds and failure behavior](system-one.md).

Before any provider call, run the bounded offline mock checks:

```sh
pokegent provider check --config /absolute/private/agent-model.yaml --no-stub
```

Without `--live`, this validates configuration and runs in-process mock decisions,
including invalid-choice refusal, with five-second decision budgets and no network.
It does not test the configured model. To deliberately make a real model call, use:

```sh
pokegent provider check --config /absolute/private/agent-model.yaml --live --no-stub
```

The probe checks one logical decision (transient attempts may retry); health and
decision have separate bounds. `--live` opts into those calls. The probe does not
require the run command's `--allow-remote` flag.

For the loopback model endpoint above, start a run with your private inputs:

```sh
pokegent run --config /absolute/private/agent-model.yaml \
  --rom "$FIRERED_ROM_PATH" --state /absolute/private/start.state \
  --max-steps 5 --out /absolute/private/model-demo
```

For a hosted compatible model, set `system1.base_url` to its HTTPS endpoint (for
example, `https://provider.example/v1`) and its supported `request_model`.
Non-loopback model endpoints are refused by `run` unless you explicitly opt in:

```sh
pokegent run --config /absolute/private/agent-model.yaml --allow-remote \
  --rom "$FIRERED_ROM_PATH" --state /absolute/private/start.state \
  --max-steps 5 --out /absolute/private/remote-model-demo
```

`--allow-remote` permits calls to the configured remote model; the agent bridge
still binds to loopback. No real provider or emulator acceptance is claimed by
the offline checks.
