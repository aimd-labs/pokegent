# Configuration

Copy `pokegent.example.yaml` to `pokegent.yaml`. The agent loads that file when
present, then applies environment variables, then explicit CLI flags. The real
`pokegent.yaml` is ignored by git. Pass another file with `--config PATH`.

The main keys are `rom_path`, `decider` (`agent`, `system1`, `hybrid`, `first`, or
`forced`). The one-release `external` alias runs `agent` with a deprecation warning.
`system1.base_url`, `system1.api_key` or `system1.api_key_env`, optional
`system1.model` and `system1.timeouts`, `external.port` (19700–19799) and
and `hybrid.escalate_below_confidence` (default `0.35`, or a mapping of `scene_id` to thresholds
with optional `default`; unknown scenes use `default`, or `0.35` when omitted),
`hybrid.override_window_s` (default `0`). `battle.opponent_knowledge` (`guide` by default or
`fair_play`), `field.menus`, and `output.run_dir`.

The runner has no self-recovery ladder. On no progress it prints one `stopped: no progress ...`
line and stops; an escalation makes it wait for its agent (agent and hybrid mode) or stop with
`escalated` (exit 3) in system1 mode. Old `recovery:`, `objectives:` and `runner.busy_nudge_after`
keys are rejected with an error, never silently ignored.

Environment overrides include `POKEGENT_ROM_PATH`, `POKEGENT_DECIDER`,
`SYSTEM1_BASE_URL`, `SYSTEM1_API_KEY`, `SYSTEM1_MODEL`,
`POKEGENT_EXTERNAL_PORT`, `POKEGENT_FIELD_MENU`,
`POKEGENT_HYBRID_ESCALATE_BELOW_CONFIDENCE`,
and `POKEGENT_HYBRID_OVERRIDE_WINDOW_S`. API keys are never printed by the configuration layer;
prefer `api_key_env` so the secret is not stored in the file.

Compatible TM/HM teaching options are always available; legal game actions are not hidden behind
configuration or objective relevance. `POKEGENT_FIELD_MENU` still controls non-teaching field-item
options. Teaching verifies the learned move and consumes an ordinary TM while retaining an HM.

`hybrid.escalation_timeout_s` and `POKEGENT_HYBRID_ESCALATION_TIMEOUT_S` were removed;
`external.timeout`, `POKEGENT_EXTERNAL_TIMEOUT`, and `--external-timeout` were removed because
agent choices wait indefinitely.
supplying either is an error because escalated decisions wait for the agent indefinitely.

## Graceful stop

`run.graceful_stop_timeout_s` is the hard graceful-stop budget (default `10`). SIGINT, SIGTERM, or
creation of `run.restart_file` cancels an in-flight System One HTTP request or external-decider
wait, then stops before the next input and atomically writes the validated `final.state`. A restart
marker left over from an earlier process is removed at startup and ignored.

## Cold-boot new game

`pokegent run --new-game` boots the ROM in-process and drives the title/new-game flow without a
state file. Only explicitly identified pre-game scenes are accepted before FireRed creates the
avatar; ordinary `--state` runs retain the strict valid-avatar startup gate. Gender defaults to boy,
and the name screens choose the game's first preset rather than typing a custom name.

```yaml
new_game:
  gender: male       # male or female
```

If the run has not reached its normal save path by the deadline, the watchdog queues the same
validated save as one exclusive actor transaction, allows it at most one additional second, then
allows up to 0.5 seconds to flush and close `events.jsonl` before exiting 124.
A second SIGINT/SIGTERM takes that bounded save path immediately and exits 130. The process may
therefore exit before `summary.json` is written on either hard path; `final.state` is the recovery
artifact. Before the coordinator starts and after it closes, the process uses OS-default signal
termination so another signal cannot be consumed without an active watchdog.

Run exit codes are: 0 normal/requested stop, 1 blocked, 2 usage, 3 paused, 4 environment, 124
graceful-stop deadline, and 130 forced interrupt.

## Local game data

Set `POKEGENT_LOCAL_DATA_DIR` before startup to load validated game-data files outside the checkout.
`POKEGENT_MAP_GROUPS` optionally overrides the map index. See [local-data.md](local-data.md) for schemas,
missing-file behavior and the setup limitation until automatic local generation exists.
