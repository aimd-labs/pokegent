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

Python dependencies are installed separately through pyproject.toml and retain their own licenses.
No third-party Python package, vendored skill or raster artwork is bundled in this source tree.
