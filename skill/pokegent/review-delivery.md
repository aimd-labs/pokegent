# Read-only observer and owner delivery

Install the reviewed checkout and run the observer in a separate foreground process,
owned by the installation's process manager:

```sh
env CODE=/path/to/pokegent PY=/path/to/pokegent/.venv/bin/python \
  LIVE_HOME=/path/to/private/live-data \
  bash /path/to/pokegent/ops/pokegent_live.sh
```

The observer reads `live-run.json`, all `runs/live-NNNN/exit.json` records and incremental
`events*.jsonl`. It sees in-run `progress.milestone_completed` records with `completed: true`;
the same process keeps playing. Rotated logs are followed by inode and offset. Finished
segments are also scanned so a fast transition or offline monitor cannot hide a run end.
The observer never launches, signals, rewinds, promotes states or edits objectives.
If both a recorded child and its supervisor are dead without an exit record, it queues
a review from available facts; missing facts stay null/`none`. A live supervisor retains
ownership of the final verdict publication, including `owner_review`.

`ops/hermes/pokegent-owner-notify` reads one JSON object on stdin and durably enqueues it.
Exit `0` acknowledges **queue acceptance**, not owner delivery. Exit `75` means queue full
or, with `--drain`, deferred/failed delivery; exit `2` is invalid input/configuration or a
journal error. `--drain` attempts one batch without waiting for the owner to become idle.
The continuous observer drains asynchronously, so no separate queue worker is needed.
`ops/pokegent_live.sh --once` only observes and queues, useful for diagnostics.

| Environment | Default / meaning |
|---|---|
| `LIVE_HOME` | Required private data root, outside the checkout. |
| `CODE`, `PY` | Wrapper checkout and interpreter; same release as the installed runner. |
| `POKEGENT_OWNER_TARGET` | Unset. Persistent session target for the optional Core transport. |
| `POKEGENT_OWNER_COMMAND` | JSON argv array. If unset and a target is set, uses `hc send --if idle {target} {message}`. Otherwise delivery is disabled and reviews stay queued. `[]` explicitly disables it. |
| `POKEGENT_OWNER_MIN_INTERVAL_S` | `180`; minimum time between attempts, including busy/failed attempts. |
| `POKEGENT_OWNER_TIMEOUT_S` | `10`; positive command deadline, then kills the transport process group. |
| `POKEGENT_OWNER_POLL_S` | `2`; positive observer/delivery polling interval. |
| `POKEGENT_OWNER_QUEUE_LIMIT` | `32`; positive maximum pending reviews. |
| `POKEGENT_OWNER_BATCH_LIMIT` | `4`; positive maximum reviews per coalesced delivery. |

Core is optional. A custom adapter can use any persistent owner framework. Supply its argv
as data, for example `["/path/to/owner-adapter", "{target}"]`. No shell expansion occurs.
The target/message placeholders expand within individual arguments, and sanitized JSON
is always also supplied on stdin. Command output is discarded and never copied into a
public log or owner prompt. Credentials belong in the installation environment or private
configuration, never command arguments or this repository.

The adapter must atomically deliver **only to an idle owner**, return nonzero while busy or
on failure, and return zero only after the owner has accepted the message. A busy timeout
or merely writing to a local temporary file is not successful delivery. The built-in Core
command uses `--if idle` for this check. Delivery targets an existing session; it does not
start a new one-shot agent. The owner follows [SKILL.md](SKILL.md).

Only the five typed fields described in the skill are retained. Unknown fields, event
details, prose and provider configuration are discarded. Input is bounded to 16 KiB;
run IDs, numeric facts and state references are validated. Completion batches preserve
their individual revisions. If the queue fills, the observer leaves the event cursor or
run receipt uncommitted and retries after capacity becomes available; it does not discard
older reviews. Keep run records and rotated logs until the observer has consumed them.

The private `owner-review/queue.sqlite3` stores pending facts, event cursors, completed-run
receipts and delivery hashes. Delivery uses one file lock; acknowledged facts are removed
while their hashes remain for deduplication. Queue and receipts survive monitor restarts.
Do not delete this journal or its lock while using the same run history. Receipt metadata
grows with completed runs/revisions; archive it together with retired run data.

An external command cannot make its acceptance and the local SQLite receipt one atomic
transaction. A crash after owner acceptance but before receipt commit can repeat delivery.
Adapters needing deduplication across that window should persist `POKEGENT_REVIEW_ID`
(the batch hash exposed in their environment) and individual review identities. Otherwise
delivery retries are at-least-once across that narrow window; ordinary monitor restarts
after receipt commit do not redeliver. Timeout/failure leaves the batch pending. This
channel does not impose deadlines or fallbacks on game legality or hybrid choices.
