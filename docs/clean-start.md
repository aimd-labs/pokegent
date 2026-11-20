# Clean start with your own ROM

Local acceptance at source `5fb3734f40c8e4f2d405517e4ab5c2f06be0ad52` used a fresh
private Python 3.11 environment on Ubuntu 24.04 amd64 with existing host libraries,
the unofficial `mgba==0.10.2` binding and pinned public native packages. With offline,
hash-verified inputs, setup generated seven local files and a ROM cache. The run
reached the first field and completed three typed movements verified against current RAM.

Acceptance used a reviewed private operator for sole non-irreversible introduction
text choices. Title, gender, names and field movements were explicitly inspected.
The existing public example below prompts for every decision. This tested path
does not establish fresh-OS support, full-game coverage, reproduced native binaries
or proven filesystem confinement. The gameplay attempt did not invoke the native
helper's download command.

## Prepare the environment and local data

Complete [native setup](native-install.md) first. Use the same fresh private prefix
in both guides, replacing the `/absolute/private/` placeholders with your paths.
Keep the ROM read-only outside the checkout; do not copy it into generated data.
Review inherited project/provider environment overrides before starting.

```sh
task_prefix=/absolute/private/pokegent-native
. "$task_prefix/venv/bin/activate"
export FIRERED_ROM_PATH=/absolute/private/own-game.gba
export POKEGENT_LOCAL_DATA_DIR="$task_prefix/local-data"
export POKEGENT_ROM_DATA_CACHE="$task_prefix/rom-cache"
unset POKEGENT_MAP_GROUPS
pokegent rom-data setup --rom "$FIRERED_ROM_PATH" --out-dir "$POKEGENT_LOCAL_DATA_DIR" \
  --decomp /absolute/private/pinned-pokefirered.tar.gz
```

The [local-data guide](local-data.md#explicit-local-setup) names the exact pret
source pin, digest and offline archive/checkout rules. Local-data and cache paths
must be separate; choose absent output paths. Omitting `--decomp` permits the fixed
public source download. Preserve failed prefixes and restart Python after setup.

## Start a bounded fresh game

Write private configuration and a current objective. This objective supplies fresh
startup/naming intent without guessing a destination or a completion probe.

```sh
umask 077
cat > "$task_prefix/clean-start.yaml" <<'YAML'
decider: agent
autoplay: "off"
external: {port: 19760, walk_step_limit: 1}
field: {menus: false}
objectives: {fallback: "off"}
run:
  checkpoint_every: 0
  save_final_state: false
  on_objective_done: pause
new_game: {gender: male}
viewer: {port: null}
stream: {enabled: false}
dashboard: {enabled: false}
YAML
cat > "$task_prefix/objective.json" <<'JSON'
{
  "schema_version": "objective/1",
  "objective_id": "clean_start",
  "revision": 1,
  "kind": "hold",
  "target": {},
  "success_probes": [],
  "forbid": ["pc", "storage", "fly", "shop", "tm", "sell"],
  "rationale": "Start a fresh game only when no save exists. Use the configured gender and offered default name presets; confirm only those choices. Then make three reversible offered field movements and stop after RAM verification. Stop on unknown legality or any other irreversible prompt.",
  "set_by": "operator"
}
JSON
pokegent run --rom "$FIRERED_ROM_PATH" --new-game --no-state-fallback \
  --decider agent --autoplay off --external-port 19760 \
  --config "$task_prefix/clean-start.yaml" --objective "$task_prefix/objective.json" \
  --max-steps 160 --deadline-s 300 --out "$task_prefix/run"
```

Use a free loopback port and an absent run directory. This selects no old state,
provider or viewer. The `hold` objective has no automatic completion probe; the
operator counts verified field actions and stops the run. The deadline also bounds
decision waits. If a limit expires, record an incomplete attempt and preserve it.

## Inspect each frame and verify the result

From the checkout, in a second shell with the same environment:

```sh
python -m examples.choose_inspected_frame --port 19760
```

The [agent guide](connect-your-agent.md#agent-mode-inspect-choose-verify) explains
the actual `/briefing`, `/frame?wait=0` and `/choose` loop. Inspect the state, option
criteria and feedback, then submit an offered option with that exact inspected
`frame_id`. Reinspect for every decision; do not reuse a token or replay inputs.
Title, gender, name confirmations and field choices need current explicit intent.
Stop if CONTINUE is offered, observations are missing, legality is unknown, or an
irreversible choice exceeds the current objective. Never fill a gap with raw keys.

HTTP 202 means queued. Require the next feedback and `events.jsonl` to show the
chosen action applied with `verified: true`; the event name `action.verified`
alone can include an unverified result. Match started/verified events by their
request token and option within that choice's event window. A stale HTTP 409 or
request failure stops that submission; inspect anew before making another choice.

After a usable `field.free` frame, choose three offered reversible movements,
verifying each against RAM before the next. Introduction/menu/text actions do not
count. After the third verification, send one SIGTERM to your owned run, let it
exit and check its summary, logs and private proof. A failed/unverified action,
missing field evidence, cleanup failure or cap leaves acceptance incomplete.

The accepted check also used a 900-second whole-attempt limit with owned process
cleanup and private before/after output inventories. Moving HOME or CWD alone
does not prove where native code can write. Those source/package trust limits
remain relevant when adapting this recipe to another host or native version.
