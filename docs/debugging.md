# Debugging from event logs

`pokegent` appends one `EventEnvelope` per line to `events.jsonl` (rotated segments are
`events-NNNNNN.jsonl`); the fields are defined in `pokegent/events.py` and `docs/contracts.md`.
`tools/lnav/pokegent-events.json` is a read-only [lnav](https://lnav.org) format for those files:
it uses `observed_at` as the log timestamp and exposes `type`, `scene_id` and `outcome` as table
columns and filter variables.

Validated against lnav 0.14.1 with the official static Linux build. The format only displays
records. It does not enforce `EventEnvelope` rules: a record with a malformed `type` still parses,
so legality and schema checks stay in the runtime.

## Load a run

lnav reads formats from `<dir>/formats/<name>/*.json`, so stage the file in a scratch directory
for the invocation instead of copying it into your lnav configuration:

```sh
mkdir -p /tmp/pokegent-lnav/formats/pokegent
cp tools/lnav/pokegent-events.json /tmp/pokegent-lnav/formats/pokegent/
lnav -I /tmp/pokegent-lnav /path/to/run/events.jsonl
```

Add `-C` to check that the format matches the file and exit without starting the UI:

```sh
lnav -C -I /tmp/pokegent-lnav /path/to/run/events.jsonl
```

`lnav -i tools/lnav/pokegent-events.json` installs a copy into lnav's own configuration directory
if you want the format available in every session.

## Filter

Filter expressions see the envelope fields with a `:` prefix:

```text
:filter-expr :type = 'action.verified'
:filter-expr :type LIKE 'action.%'
:filter-expr :scene_id = 'field.free' AND :outcome IS NULL
```

`:filter-in <regex>` also matches the displayed line, for example `:filter-in runtime.`.

## Query

```sql
;SELECT type, scene_id, outcome, count(*) AS n
  FROM pokegent_events
  GROUP BY type, scene_id, outcome
  ORDER BY type
```

The table has `log_line`, `log_time`, `log_level`, `type`, `scene_id`, `outcome` and lnav's
`log_*` metadata columns. Only those three envelope fields are columns; the rest of the record
stays in the JSON body.

## Input the format does not index

- A line that is not valid JSON is shown with level `invalid` and NULL fields; later lines still
  parse.
- A JSON record without `observed_at` is not indexed by the format.
- A file that is not named `events.jsonl` or `events-NNNNNN.jsonl` is not parsed by this format;
  `-C` reports `no format found for file`.
- JSON `null` renders as `null` and a missing key as `-`; both are NULL in SQL. Timestamps are shown
  in the local time zone, so set `TZ=UTC` to read them as written.

`tools/lnav/sample/events.jsonl` is invented data for trying the format:

```sh
lnav -I /tmp/pokegent-lnav tools/lnav/sample/events.jsonl
```
