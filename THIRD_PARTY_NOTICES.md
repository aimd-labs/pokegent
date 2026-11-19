# Third-party notices

Pokegent code is MIT licensed; LICENSE preserves AIMD Labs and Nous Research notices.

The offline tools `tools/snapshot_dump.py` and `tools/ram_conformance.py` adapt the
same paths from source A, pinned at `7c8742ba50a7a482e24d69bb1364c49e63c4e453`.
They retain the MIT notices in LICENSE; live and ROM-name lookup paths are excluded.

mGBA and libmgba are optional external dependencies under MPL-2.0, installed separately.
No mGBA binary or source is bundled here. Consult the installed mGBA distribution's license
and notices before redistributing it, including in a container or packaged application.

The reader's numeric profiles, struct layouts, protocol constants and text decoder were informed
by pret/pokefirered (revision in source-lock.md). The retained material describes binary formats;
no separate grant is asserted for Pokémon game content. Exact copied map labels and game tables,
quiz answers, elevator destinations, mansion layouts and sprites are excluded. Retained offsets,
struct layouts, constants, decoder mappings and curated identifiers describe interoperability facts;
pret/pokefirered is credited as the technical reference. These facts do not include game datasets.

The explicit private local-data setup command adapts the ordered-map and static tileset layout
recipes from the earlier AIMD Labs worldgen/regiongen tooling, retaining AIMD Labs and Nous Research
MIT attribution. It reads a user's verified pinned pret source at runtime, without executing it.
No downloaded source, game text, output dataset or historical source tree is redistributed here.

Python dependencies are installed separately through pyproject.toml and retain their own licenses.
No third-party Python package, vendored skill or raster artwork is bundled in this source tree.

The opt-in objective fallback adapts generic `DexFallback` selection and objective-session
handoff code from MIT-licensed pokevon_agent, source revision
`3de37392cbbfdff3876bfed98571aa861f4c1032` (`goals/dex_fallback.py` and
`goals/source.py`). LICENSE retains the Nous Research notice. Species-name, town and
story tables, removed tactical fields and private game inputs are excluded.

The bounded `recent_choices` history and runner hooks adapt the MIT-licensed
pokevon_agent `history.py` and `runner.py`, locally pinned at revision
`7185ae105ca0317753f53e0f16eb1843028df119`. LICENSE retains the Nous Research notice.
The port excludes the repetition ledger and recovery/route-assist code.
