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
the same process keeps playing. Rotated logs are followed by logical generation (first
sequence and record hash), file identity and a checked offset boundary, so recycled inodes
cannot hide new records. Finished segments are also scanned so a fast transition or offline
monitor cannot hide a run end.
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

Only the five typed input facts described in the skill are retained; delivery adds a
generated `review_id`. Unknown fields (including caller-supplied IDs), event details,
prose and provider configuration are discarded. Input is bounded to 16 KiB;
run IDs, numeric facts and state references are validated. Completion batches preserve
their individual revisions. State references are constructed relative to `LIVE_HOME`, only
as `runs/<run_id>/final.state` or `runs/<run_id>/checkpoints/ckpt-<digits>.state` (at least
seven digits). Other path text, traversal and symlinks outside that location are dropped;
pending journal records are revalidated before delivery. If the queue fills, the observer
leaves the event cursor or
run receipt uncommitted and retries after capacity becomes available; it does not discard
older reviews. Keep run records and rotated logs until the observer has consumed them.

The private `owner-review/queue.sqlite3` stores pending facts, event cursors, completed-run
receipts and delivery hashes. Delivery uses one file lock; acknowledged facts are removed
while their hashes remain for deduplication. Queue and receipts survive monitor restarts.
Do not delete this journal or its lock while using the same run history. Receipt metadata
grows with completed runs/revisions; archive it together with retired run data.
Upgrading from inode-only cursors rescans retained logs into the existing deduplicated
review queue; the old cursor table is left intact for rollback.

Delivery is **at-least-once**. Every review includes a stable `review_id` in both the
delivered owner text and stdin JSON, derived from run ID, kind (`objective_completion`
or `run_end`) and objective revision (null if unknown). The notifier queues `run_end`
reviews; the event observer queues `objective_completion` reviews. Retries preserve
individual IDs even if batching changes. The same review id may arrive twice; treat it
as one, as directed by the owner skill. Input cannot supply or override this ID.

An external command cannot make its acceptance and the local SQLite receipt one atomic
transaction. A crash or receipt error after acceptance, or a timeout with an ambiguous
outcome, leaves the review pending and retries the same ID. Ordinary monitor restarts
after receipt commit do not redeliver. Custom adapters may also persist IDs to suppress
duplicate processing. `POKEGENT_REVIEW_ID` is a batch hash supplied in the transport
environment for convenience; it may change when pending reviews are rebatched, so use
the individual `review_id` for deduplication. This channel does not impose deadlines or
fallbacks on game legality or hybrid choices.
