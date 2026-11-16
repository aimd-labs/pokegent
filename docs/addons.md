# Optional local addons

There are exactly three optional addons:

* `pokegent/dashboard/stream` captures the emulator picture and sound. It serves `/frame.png`,
  `/ws/frames`, and `/ws/audio` (PCM signed 16-bit little-endian, stereo, 32,768 Hz).
  The audio path is a bounded ring buffer; slow WebSocket clients cause drops, never game-loop
  backpressure. It also has `/health`; it does not serve dashboard or telemetry routes.
* `pokegent/dashboard` serves only the operator/public HTML, telemetry, decision history, and a small
  WebAudio player. Its page consumes picture and sound from the separately-owned stream listener;
  it has no `/frame.png`, `/screenshot`, `/ws/audio`, or frame hub. It contains no Nintendo or
  third-party sprite assets.
* `addons/system_one` is the optional decision provider; its hybrid decider lives in `addons/system_one/hybrid.py`.

Enable the services on separate loopback ports:

```sh
pokegent run --state path/to/run.state --stream-port 19700 --dashboard-port 19701
```

`--viewer-port 19700` remains a one-release compatibility alias that enables both surfaces on
one port. YAML can use `stream:` and `dashboard:` sections with `enabled: true` and a port from
`19700` through `19799`. All addon listeners bind `127.0.0.1`; remote binds and public tunnels
are deliberately unsupported. `--viewer-port` is deprecated; `--viewer-host` and
`--viewer-allow-remote` have been removed. If only `dashboard` is enabled, it starts a separate
stream listener on the adjacent loopback port when available, and otherwise the page says
`stream off`.

When the dashboard and stream use separate ports, the stream accepts browser media requests only
from the exact paired dashboard origins `http://127.0.0.1:<dashboard-port>` and
`http://localhost:<dashboard-port>`. It returns the matching `Access-Control-Allow-Origin` and
`Cross-Origin-Resource-Policy: same-site` headers for those origins; foreign origins remain
blocked. This is a narrow local pairing, not general CORS.

decider. It is an operator-only route on the local loopback dashboard, for the person who owns the
run; it is not part of any public surface. `/telemetry`, `/agent/live`, and `/ws` are the public
dashboard surface. Do not expose either listener through a tunnel or proxy.

Public telemetry's `pokegent.decision` (also `pokejev.decision`) includes `decided_by`:
`"system1"` or `"agent"` for an accepted hybrid decision, otherwise `null` (including
non-hybrid runs and decisions still being considered). `awaiting_agent` is a boolean,
true only while a hybrid escalation is waiting for the agent's choice; it is false
after the call returns or the run stops. A non-escalated override window does not
set it. Existing `source` and `source_label` fields retain their meanings.

The old upstream REST server and its mutable `/action`, `/save`, `/load`, and `/control` API are
removed. The run interface remains read-only, and the external `/screenshot` contract is unchanged.
The one-release compatibility implementation in `addons/runtime.py` is the only addon listener
that mounts `/screenshot`; explicit stream and dashboard listeners do not expose it.
