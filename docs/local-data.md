# Local game data

The live stream runs this public code with private local game data kept outside the repository.
No ROM, extracted dataset, sprites or run recordings are bundled. Bring your own legally obtained
supported ROM. Automatic local generation from your own ROM is planned; without supplied data, this checkout
supports setup and synthetic checks but not a complete emulator run.

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

The first six filenames retain the earlier runtime's document schemas. Extra provenance fields
such as `rom_profile`, `verified_by`, `schema`, `rom` and `provenance` are accepted. All required
fields, ranges and enum values are in [local-data-schemas.json](local-data-schemas.json).
Coordinates and map keys are two-element integer arrays; map IDs are bytes. Booleans must be
actual JSON/YAML booleans, not strings. Keys listed below are required unless marked optional.

| Filename | Required document shape and runtime use |
|---|---|
| `pret_map_groups.json` | `group_order: [group_name, ...]`, `groups: {group_name: [map_label, ...]}`. Order and keys must agree; group and map labels are unique. Array positions are the ROM's group/number IDs. Used by map reads, named objectives, cache building and Fly validation. |
| `cinnabar_quiz.yaml` | `questions: [{number, prompt, answer_yes, door_flag}, ...]`. Unique positive `number`, nonempty `prompt`, boolean `answer_yes`, integer or hexadecimal-string `door_flag`. Supplies quiz facts and dialog answer/door metadata. |
| `elevators.yaml` | `elevators: {name: {scene, map, menu, panels, exit_mat, exit_push, floors, cancel, ...}}`. `scene` is `dialog.elevator` for `multichoice`, `menu.elevator` for `list`. `panels` is a list of coordinate pairs. `exit_push` is `up/down/left/right`. Each floor is `{code, label, map, place, arrival?}`. Optional `menu_id`; list menus require `total` equal to floor count and positive `shown <= total`. Optional `requires: lift_key`. Up to 12 floors. Supplies the existing elevator detectors, builders, macros and verifiers. |
| `field_surf.yaml` | `capability: {move: {name, move_id}, badge: {number, name, flag}, prompt: {text}}`, `start_rule: {player_elevation, front_tile, result, water_behaviors}`, `dismount_rule: {land_elevation, water_elevation, rule}`. Retains the reference facts used by Surf diagnostics; normal Surf capability and movement continue to read RAM/ROM as before. |
| `puzzles.yaml` | `mansion: {switch_flag, maps, secret_key, statues}`. `maps` maps floor names to map pairs; `secret_key: {map, tile, hidden_item_flag}`. Each statue is `{id, floor, map, statue, stand, facing, opens, closes}` with unique ID, matching floor/map, `facing: N/S/E/W`, and nonempty `opens`/`closes` lists of nonempty lists of tile pairs. Supplies mansion registration, switch facts and RAM verification. |
| `prompt_rules.yaml` | `rules: [{kind, patterns, default_yes?, field_move?, irreversible?, wrong_answer_cost?, metadata?}, ...]`. `patterns` are nonempty literal text fragments, matched after case/accent/whitespace normalization. `kind` uses the `PromptKind` enum in the JSON schema. Rules supplement generic action recognition; destructive and unreadable-state protections still win. Quiz questions can also supply rules without this file. |
| `fly_destinations.json` | `destinations: [{mapsec, name, map_key, landing, visited_flag, cell}, ...]`. This new file mirrors the old `FlyDestination` structure: `map_key`, `landing` and `cell` are pairs; IDs/flags are nonnegative integers. Map section, name and map key must each be unique; `name` must match the corresponding local map-index label (case/underscores ignored). Restores destination lookup and existing visited-flag/routing logic. |

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
