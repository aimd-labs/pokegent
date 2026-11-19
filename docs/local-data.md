# Local game data

The live stream runs this public code with private local game data kept outside the repository.
No ROM, extracted dataset, sprites or run recordings are bundled. Bring your own legally obtained
supported ROM. Complete first-start onboarding remains planned; the explicit generator below
supplies local data, while native emulator/agent acceptance is a separate gate.

## Explicit local setup (generator candidate)

`rom-data setup` now generates the seven files and builds the private ROM cache. Complete
first-start emulator onboarding remains under validation; generation/cache success alone does
not establish a successful emulator run. Use Python 3.11 and your own supported FireRed USA Rev 0
ROM, outside this checkout. Native mGBA installation and the agent bridge are separate prerequisites.

```sh
export FIRERED_ROM_PATH=/absolute/private/own-game.gba
export POKEGENT_LOCAL_DATA_DIR=/absolute/private/pokegent/local-data
export POKEGENT_ROM_DATA_CACHE=/absolute/private/pokegent/cache
unset POKEGENT_MAP_GROUPS
pokegent rom-data setup --rom "$FIRERED_ROM_PATH" --out-dir "$POKEGENT_LOCAL_DATA_DIR"
```

Choose an absent absolute output path. Setup never modifies a mismatching existing directory,
including an empty/manual one: choose a new path instead. It writes the complete seven-file set
and a small `setup-manifest.json` in a private staging directory, then publishes the directory
atomically. A competing populated destination makes the rename fail; this does not claim portable
`RENAME_NOREPLACE` semantics against a racing empty directory. Directories are created 0700 and
local files 0600. User-controlled parent paths are assumed, as for the numeric index tool.

After local publication, a fresh Python process builds or validates the cache with the selected
local directory and without a map-index override. A cache failure leaves complete local data intact
and returns failure. Rerun the same setup to repair the cache. Identical existing files, manifest,
generator version, source identity and ROM identity skip source fetching and local generation,
preserving local file mtimes. Changed/corrupted local output is preserved and refused. Runtime
tables are loaded once per process: restart Python/runtime processes after setup.

Online setup downloads only the fixed pret/pokefirered gzip tarball at
`037335f4c725d7c9aecdac87066f2002b4bd7e14`, checks its complete content identity, and caches
the verified archive under `$POKEGENT_ROM_DATA_CACHE/sources/`. It never executes or extracts
decompilation code. The transfer has a 60-second total deadline, a 10-second per-operation timeout,
and a 64 MiB compressed cap. Source reads are limited to 20,000 members and 128 MiB expanded,
with at most 2 MiB per retained extractor input. Links, duplicate/noncanonical names and unexpected
archive roots are rejected. Rejected source values are not printed.

For zero network access, supply a clean exact-pin checkout or gzip archive:

```sh
pokegent rom-data setup --rom "$FIRERED_ROM_PATH" --out-dir "$POKEGENT_LOCAL_DATA_DIR" \
  --decomp /absolute/private/pokefirered
```

A dirty or built checkout fails its tree digest; Git's reported HEAD alone is insufficient.
Use a separate clean source tree rather than deleting unrelated files. No hook, build command,
preprocessor or included Python is run. ZIP archives and moving branches are not accepted.

The canonical tree digest is SHA-256 of `pokegent-decomp-tree-v1` followed by NUL, an unsigned
64-bit big-endian regular-file count, then records sorted by the UTF-8 bytes of relative POSIX
paths. Each record is an unsigned 32-bit big-endian path-byte length, those exact bytes, and the
raw 32-byte SHA-256 content digest. Strip only the archive's fixed `pokefirered-<commit>/` root;
exclude root `.git` metadata for both readers. Include all other regular bytes, including files
unused by the extractors. Directories, mode bits, timestamps and gzip encoding do not enter the
digest; no Unicode normalization is applied. The pinned tree has 9,857 regular files and digest
`df2f96d640e48ccf62db35d14519f4efd798773f0e9352965b168a845bea526e`.

The cache manifest is format 3: it includes the exact selected map-index content digest as well
as ROM/generator/artifact identity. A changed named index or old cache manifest triggers rebuild.
`pokegent rom-data build --if-missing --rom PATH` exposes this validation/repair path; ordinary
`rom-data build` continues to rebuild explicitly.

Generated documents use source-correct quiz text, source map labels for elevator descriptions,
and source-labelled prompt policies. They do not reproduce historical measured annotations or
the unused Silph section of an older puzzles document. The seven-file format and required mansion,
Fly, elevator, quiz and Surf facts are preserved. Generic built-in prompt recognition and destructive
and unreadable-state precedence remain in force. Source messages for unsupported field features
do not register new actions or grant capability. See the source attribution in `source-lock.md`.

## Configure before startup

Set one directory for all seven files, keeping their exact names:

```sh
export POKEGENT_LOCAL_DATA_DIR=/path/to/your/local-game-data
export FIRERED_ROM_PATH=/path/to/your/FireRed.gba
```

The directory and ROM must stay outside the checkout. `POKEGENT_MAP_GROUPS` remains an optional
override for the map-index file only, and takes precedence over the directory. Configure these
variables before starting Python or the CLI: tables are loaded once per process; restart after
changing files. Generated ROM caches also belong outside the checkout.

Each supplied JSON/YAML file is schema validated before use. Invalid content raises a
`LocalDataError` (a `RomDataError` for map lookup), with no rejected values in the error message.
A missing optional file disables its feature; missing data requested directly produces an
explanatory error. A missing map index stops map operations and cache building. Schemas prove
structure, not agreement with the game: use verified data for the exact supported ROM revision.
No map label, answer or destination is inferred from model prose.

## File formats

### Generate a numeric structural index

For the exact supported FireRed USA Rev 0 image, a local generator can create an index for
numeric map diagnostics. It uses labels such as `Map_G000_N000`, with `label_kind: numeric-id-v1`
and `named_compatibility: false`. These are generated ID tokens, not recovered pret names.
The metadata documents the kind; existing loaders do not enforce compatibility from it.

```sh
mkdir -m 700 /absolute/private/map-index-ids
pokegent rom-data map-index --numeric-labels --rom /absolute/private/game.gba --out-dir /absolute/private/map-index-ids
env -u POKEGENT_LOCAL_DATA_DIR POKEGENT_MAP_GROUPS=/absolute/private/map-index-ids/pret_map_groups.json python tools/map_dump.py --rom /absolute/private/game.gba --map 0 0
```

Supply an existing empty absolute output directory outside source checkouts. The command verifies
the complete input against the packaged profile and writes only `pret_map_groups.json`; it refuses
existing output, symlinks at final paths and mixed/nonempty output directories. No overwrite flag
exists. Parent paths are resolved for checkout containment; this unprivileged tool assumes
user-controlled local paths, not a hostile process racing ancestor paths. Atomic no-clobber
publication preserves an existing target even if it appears during generation. Errors before
publication leave existing files unchanged; a postpublication durability/cleanup warning means the
complete new artifact exists, with no rollback claim. The warning retains success exit status.

Use a fresh diagnostics process with `POKEGENT_LOCAL_DATA_DIR` unset and the explicit index override.
Manual named indexes and their consumers retain their existing behavior. Generated tokens work in
the existing string-based objective resolver; historical names receive no new aliases. Combining
this numeric override with a mismatched named Fly sidecar raises `LocalDataError` during import,
rather than silently disabling Fly. Other sidecars retain their existing validation; there is no
blanket mixed-data guard. No sidecars are generated. This slice does not provide named-data/cache
compatibility, Fly or special-action tables, or a complete emulator run. Issue #1 remains open;
synthetic checks do not demonstrate real-game capability.

The first six filenames retain the earlier runtime's document schemas, except that the diagnostic
Surf `water_elevation` may be omitted or null when unknown; legacy numeric values remain valid.
Extra provenance fields
such as `rom_profile`, `verified_by`, `schema`, `rom` and `provenance` are accepted. All required
fields, ranges and enum values are in [local-data-schemas.json](local-data-schemas.json).
Coordinates and map keys are two-element integer arrays; map IDs are bytes. Booleans must be
actual JSON/YAML booleans, not strings. Keys listed below are required unless marked optional.

| Filename | Required document shape and runtime use |
|---|---|
| `pret_map_groups.json` | `group_order: [group_name, ...]`, `groups: {group_name: [map_label, ...]}`. Order and keys must agree; group and map labels are unique. Array positions are the ROM's group/number IDs. Used by map reads, named objectives, cache building and Fly validation. |
| `cinnabar_quiz.yaml` | `questions: [{number, prompt, answer_yes, door_flag}, ...]`. Unique positive `number`, nonempty `prompt`, boolean `answer_yes`, integer or hexadecimal-string `door_flag`. Supplies quiz facts and dialog answer/door metadata. |
| `elevators.yaml` | `elevators: {name: {scene, map, menu, panels, exit_mat, exit_push, floors, cancel, ...}}`. `scene` is `dialog.elevator` for `multichoice`, `menu.elevator` for `list`. `panels` is a list of coordinate pairs. `exit_push` is `up/down/left/right`. Each floor is `{code, label, map, place, arrival?}`. Optional `menu_id`; list menus require `total` equal to floor count and positive `shown <= total`. Optional `requires: lift_key`. Up to 12 floors. Supplies the existing elevator detectors, builders, macros and verifiers. |
| `field_surf.yaml` | `capability: {move: {name, move_id}, badge: {number, name, flag}, prompt: {text}}`, `start_rule: {player_elevation, front_tile, result, water_behaviors}`, `dismount_rule: {land_elevation, water_elevation?, rule}`. Surf diagnostics only; normal Surf capability and movement continue to read RAM/ROM as before. |
| `puzzles.yaml` | `mansion: {switch_flag, maps, secret_key, statues}`. `maps` maps floor names to map pairs; `secret_key: {map, tile, hidden_item_flag}`. Each statue is `{id, floor, map, statue, stand, facing, opens, closes}` with unique ID, matching floor/map, `facing: N/S/E/W`, and nonempty `opens`/`closes` lists of nonempty lists of tile pairs. Supplies mansion registration, switch facts and RAM verification. |
| `prompt_rules.yaml` | `rules: [{kind, patterns, default_yes?, field_move?, irreversible?, wrong_answer_cost?, metadata?}, ...]`. `patterns` are nonempty literal text fragments, matched after case/accent/whitespace normalization. `kind` uses the `PromptKind` enum in the JSON schema. Rules supplement generic action recognition; destructive and unreadable-state protections still win. Quiz questions can also supply rules without this file. |
| `fly_destinations.json` | `destinations: [{mapsec, name, map_key, landing, visited_flag, cell}, ...]`. This new file mirrors the old `FlyDestination` structure: `map_key`, `landing` and `cell` are pairs; IDs/flags are nonnegative integers. Map section, name and map key must each be unique; `name` must match the corresponding local map-index label (case/underscores ignored). Restores destination lookup and existing visited-flag/routing logic. |

Generated Surf diagnostics use an ordinal `Badge <number>` label, authored `front_tile`/`result`
descriptions and `rule` prose interpolating the extracted land elevation. `water_behaviors` is
empty and unknown `water_elevation` is omitted: the pinned source proves neither a behavior-ID
whitelist nor a fixed water elevation. Move ID, badge flag/number, player/land elevations and
prompt text are extracted from source; the move display label is authored. These diagnostic
fields do not control action admission.

To convert the old six-column Fly rows `(mapsec, name, constant, landing, flag, cell)`, resolve
`constant` against your verified map index, write its group/number as `map_key`, and rename `flag`
to `visited_flag`. Do this locally; do not commit the resulting file.

A schema illustration using invented rooms:

```json
{"group_order": ["Demo"], "groups": {"Demo": ["DemoArena", "DemoExit"]}}
```

These labels and dimensions are synthetic and cannot be used for a real game. The tests create
all seven documents with invented facts in temporary directories and exercise loaded lookup,
Fly availability, special-action registration, quiz classification, mansion facts and Surf rules.
No test fixture contains a real game dataset.

The scene catalog is shipped interface vocabulary. `docs/viewer/synthetic-telemetry.json` is
an authored demo stub, with no game/run extraction. Keep all local game-data files out of PRs;
the ROM/binary guard rejects these filenames in the public tree.

The guard also inspects Python ASTs for literal numeric tables containing at least 64 distinct
integer payload IDs, including nested integer-keyed collections and literal `frozenset` wrappers.
Named protocol/struct fields, text decoders and repeated small layout permutations do not match
this shape. This is a content-review tripwire: smaller tables, symbolic/computed data and game
text still require manual review. A clean guard result does not establish data provenance.
