# Continuous segment supervision

`ops/pokegent_supervisor.sh` runs installed `pokegent run` segments in the foreground.
Use a POSIX process manager to own its lifetime. Install the checkout with the extras your
configuration needs; no emulator or provider is bundled by the supervisor. All game data,
configuration, objectives, saves and run output belong outside the checkout.

Create a private data directory containing `run.yaml` and `current.state`. The latter is a
**text pointer** containing the absolute path of a reviewed, nonempty initial savestate, followed
by a newline. It is not the save itself. The runner performs native/ROM/RAM validation before
input. Never bootstrap the pointer by choosing an older save to bypass a validation failure.
The supervisor always passes `--no-state-fallback` so startup refuses a rejected selected save;
standalone `pokegent run` retains checkpoint fallback unless this flag is supplied.

```sh
env CODE=/path/to/pokegent PY=/path/to/pokegent/.venv/bin/python \
  LIVE_HOME=/path/to/private/live-data DECIDER=system1 \
  bash /path/to/pokegent/ops/pokegent_supervisor.sh
```

The shell entry point changes to `CODE` (default: its containing checkout) and executes `PY`
(default: `python3`) with `-m pokegent.supervisor`. The Python module is also directly executable.
The default runner is the installed `pokegent` executable beside that interpreter. Source,
interpreter and installed console script must come from the same reviewed release. Configuration
is inherited environment data; no shell profile, environment file or plan selector is executed.
Keep provider credentials in the process environment or the private YAML's environment references.

| Environment | Default / meaning |
|---|---|
| `LIVE_HOME` | Required; private data root. Relative values resolve from `CODE`. |
| `RUN_CONFIG` | `$LIVE_HOME/run.yaml`; must exist. Existing user YAML schema. |
| `OBJECTIVE_FILE` | `$LIVE_HOME/objective.json`; may be absent, as with `pokegent run`. |
| `OBJECTIVE_PLAN` | Unset: no plan. If set, passed unchanged as `--objective-plan`. |
| `ON_OBJECTIVE_DONE` | `continue`; passed as `--on-objective-done`. Also accepts `hold` or `pause`. |
| `DECIDER` | `system1`, an explicit continuous-play policy; accepts all run CLI deciders. Hybrid requires a working per-choice agent consumer. |
| `ALLOW_REMOTE` | `false`; explicitly enable remote System One endpoints. |
| `PORT` | Unset or `0`: no viewer CLI override. Set `19700`–`19799` for the combined viewer. Private YAML can still configure addons. |
| `MAX_STEPS` | `3000` per segment, positive integer. |
| `ALLOW_CHECKPOINT_RECOVERY` | `false`; opt in only after reviewing checkpoint recovery policy. |
| `STOP_ON_CLEAR` | `true`; stop on the summary's RAM-backed `game_cleared: true`. |
| `MAX_WHITEOUTS` | `10` consecutive segments with whiteout scene evidence; `0` disables the limit. No campaign/plan switching. |
| `MAX_STALLED_SEGMENTS` | `3` consecutive SAFE_PAUSE/no-progress segments without durable progress; positive integer. Stops with verdict `owner_review` at the limit. |
| `MAX_CRASHES` | `5` crashes in `CRASH_WINDOW_S`; positive integer. |
| `CRASH_WINDOW_S` | `600` seconds, measured with a monotonic clock. |
| `CRASH_BACKOFF_S`, `CRASH_BACKOFF_MAX_S` | `10`, `60` seconds; exponential backoff capped at the maximum. |
| `SEGMENT_DELAY_S` | `1` second between ordinary segments, including objective completion. |
| `STOP_TIMEOUT_S` | `15` seconds before killing an unresponsive child and blocking. Allow time for the runner's graceful deadline and emergency-save allowance. |
| `POLL_INTERVAL_S` | `0.2` seconds for signals/markers. |
| `RUNNER_BIN` | Installed console script beside `PY`; one executable path, no shell expansion. Useful for synthetic tests. |

Boolean settings accept `true`/`false` or `1`/`0`. Timing values must be finite and nonnegative;
the polling, stop and crash-window budgets must be positive. Relative file overrides resolve from
the launch working directory. Runner checkpoint and final-save settings remain in `run.yaml`:

```yaml
run:
  checkpoint_every: 25
  checkpoint_keep: 6
  save_final_state: true
```

The supervisor defaults to `--on-objective-done continue` so the runner keeps playing after
completion and reads newer objective revisions on the fly. Use a runner release with the
completion-clear-and-continue policy to clear the completed objective in the same process.
`ON_OBJECTIVE_DONE=hold` explicitly waits without input; `pause` ends the segment at completion.
Completion or revision change never stops the supervisor. A legacy segment ending with
`objective_done` also continues the chain. The supervisor
never clears, replaces or impersonates the writer of an objective; a compare-and-set/authority
conflict cannot trigger a clear or terminate the chain. `active_objective_revision` records the
ended summary's revision; `objective_revision` records the file's current revision in run metadata.
Runner budgets still bound individual segments.

Create `$LIVE_HOME/restart` to gracefully interrupt the current segment and continue from its
new final save. Create `$LIVE_HOME/stop`, or signal the supervisor with SIGTERM/SIGINT, to save and
end the chain. A stale restart marker is consumed at supervisor startup. Stop takes precedence
over restart; observed stop markers are consumed when stopping. Requests also interrupt backoff.
An unresponsive child is killed after the configured timeout; that condition requires review.

`live.lock` uses a nonblocking exclusive file lock. The runner inherits it, so an orphaned child
still excludes another supervisor. Never delete/replace the lock file. This protects cooperating
launchers using the **same data root**; it cannot prevent an independently launched emulator from
writing the same game. Stop and reconcile existing writers before adopting this launcher. A
recorded live PID whose ownership cannot be proven blocks startup; the supervisor never signals
a PID read from disk. A dead recorded child can be recovered only from that run's own published
state. Recovery applies `STOP_ON_CLEAR` before allocating or launching another segment; a
previously finished clear also retains this policy on restart. Missing/invalid metadata,
rejected saves and blocked records require owner review.

Each segment gets a new `runs/live-NNNN/` directory and a sibling `live-NNNN.log`. Existing run
directories are never reused even if `counter` is stale. `live-run.json` records preparing/running/
finished/blocked status, child/supervisor PIDs, run/output/input identity, times, objective revisions,
verdict, actual exit code and successor state. `runs/<id>/exit.json` retains a small completion
payload for independent observers, including runs that crashed without a summary. `journal.log`
records lifecycle decisions. Metadata, counters and `current.state` are flushed to a temporary
file, atomically renamed and directory-synced, so readers see complete documents.

Continuity prefers the current segment's runner-accepted `final.state`. If there is no summary
(e.g. the runner's hard deadline), its atomic, nonempty final publication can still be adopted.
With explicit checkpoint recovery enabled, a missing final may use the newest accepted checkpoint
from **this segment**, or its newest atomic checkpoint if there is no summary. A missing accepted
final, rejected/empty final, missing newest checkpoint or symlink blocks instead of selecting an
older save. Native save validation is performed by the runner, not duplicated by the supervisor.
No successor means no retry from the old pointer. A blocked `live-run.json` is preserved; after
reconciliation, archive it and set a reviewed pointer before restarting. Do not delete run history.

Run exit codes `2`/`4` stop for usage/environment review. Other abnormal exits and absent summaries
count toward the crash window **only when a fresh successor exists**. Controlled interruption is
neither a crash nor game clear. Crash/whiteout counts are scoped to one supervisor process; process
manager restart limits should also be bounded. Automatic process-manager restarts must not bypass
owner review of a blocked record.

**Bounded restart policy (supported).** A supervised `systemd` unit must bound auto-restarts and must
not auto-revive the exit codes that mean "a human must review". Ship the drop-in
[`ops/systemd/pokegent-live-dropin.conf`](../ops/systemd/pokegent-live-dropin.conf) (or the equivalent
`systemd-run` `-p` flags): `StartLimitIntervalSec=600` + `StartLimitBurst=3` bounds the empty-port
restart spin (observed 29x, issue #33), and `RestartPreventExitStatus=3,4,78` keeps the supervisor's
owner-review exits (3 = stalled `owner_review`, 4 = blocked/crash-limit, 78 = provider hard-abort)
from being auto-restarted into a loop that keeps re-reading the same blocked `live-run.json`.

SAFE_PAUSE/no-progress segments may resume from their accepted successor until
`MAX_STALLED_SEGMENTS` is reached. Executor action-result events with `progressed: true` reset
the streak, even when the segment later pauses; verified taps, position/scene changes and valid
saves alone do not. An ordinary non-stalled segment also ends the consecutive streak. The count
is persisted in lifecycle metadata, including while a child runs, and restored on supervisor
restart. At the limit both lifecycle metadata and the segment's exit payload publish
`verdict: owner_review`, the lifecycle status is blocked, and the supervisor exits `3`.
No later segment starts until the owner reconciles the blocked record. A separate observer owns
notification delivery.

Run `ops/pokegent_live.sh` as a separate observer to queue in-run objective completions and
run-end reviews for a persistent idle owner. Delivery is at-least-once: each review carries
a stable `review_id` derived from run ID, kind and objective revision. Ambiguous outcomes
retry the same ID; the same review id may arrive twice, so treat it as one. See the optional,
provider-neutral
[owner skill](../skill/pokegent/SKILL.md) and
[delivery configuration](../skill/pokegent/review-delivery.md).
Unknown or older private queue schemas stop the observer with an explicit error. The operator
must delete `$LIVE_HOME/owner-review/queue.sqlite3` before restarting; no migration is attempted.

`pokegent.live.verdict(run_dir)` and `payload(metadata, run_dir, verdict_name=None)` are shared by
supervisors and observers. `ops/pokegent_live_verdict.py RUN_DIR` prints `clear`, `whiteout`,
`objective_done`, `interrupted`, `owner_review`, `other` or `none` (missing/unreadable summary).
`ops/pokegent_live_verdict.py --payload METADATA_JSON RUN_DIR VERDICT` prints only run ID, verdict,
actual rc (from `exit.json` when available), successor path and active objective revision. Whiteout
uses `scenes_entered["system.whiteout"]`; clear uses the top-level game-clear fact, with no campaign
objective identifiers. Interruption takes precedence. Notification and agent ownership are separate
from this writer lifecycle; the supervisor executes no notifier command.
