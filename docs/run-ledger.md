# Local run ledger

The ledger is an opt-in local record. It does not start the game, load a ROM, or call a model.
Keep its SQLite database on a local filesystem with WAL support. The default is
`$LIVE_HOME/ledger.sqlite3` (set with `--live-home` or `LIVE_HOME`); `--db FILE` selects an
explicit database. Query commands open an existing database read only and never create or
migrate one. New database files use mode `0600`; existing files are not chmodded.

```sh
pokegent ledger status --save SAVE_ID
pokegent ledger incidents --save SAVE_ID
pokegent ledger milestones --save SAVE_ID --json
pokegent ledger sql 'SELECT id,mode,rom_profile FROM saves'
pokegent ledger splits --save SAVE_ID
pokegent ledger baseline --save SAVE_ID --group ROUTE_LABEL --game-origin-seconds 0
```

Status, incident, milestone, split, and SQL commands read the ledger. SQL is bounded and
read only. `baseline` writes an explicit comparison identity to an existing ledger. It asserts
that saves with this group share a start, route, clock origin, and rollback policy. The save's
mode and ROM profile must be known. Identity cannot be changed after configuration. Set it only
when the operator can make that assertion; the CLI does not infer a group or origin.

Splits compare first-observed milestone clocks against compatible saves in the same group,
excluding the selected save as its own reference. A clock decrease makes that save ineligible
until a new scoped attempt is chosen. The first observation bounds when a milestone was seen;
it does not establish its exact acquisition time. Missing clocks and references remain null
with an explanation. Historical possession does not prove current inventory or that an HM was
learned.

Ingestion is a separate explicit operation, independent of the game loop. It accepts
`--run-dir DIR` or `--runs-dir DIR`, and `--max-lines N` bounds each pass so ingestion can resume.
The optional `--viewer-port PORT` or `--telemetry-file FILE` supplies fresh paired run telemetry;
`--story-flag ID` selects additional flags. It uses no model and does not copy a ROM. Older
telemetry without a typed observation block cannot reconstruct RAM history. For backfill, rerun
bounded ingestion until complete event lines are caught up; a partial final line stays pending.
Save and segment identities are explicit. Stored references remain data and are never opened as
paths. The repository's [`ops/pokegent-ledger.timer.example`](../ops/pokegent-ledger.timer.example)
shows an operator-managed timer. Hermes owns any installation or enablement; this CLI does not
deploy it.
