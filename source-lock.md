# Source provenance

The project retains AIMD Labs and Nous Research MIT notices in LICENSE.
Emulator infrastructure comes from the earlier AIMD Labs runtime (MIT fork of
NousResearch/pokemon-agent). Its emulator code is Nous Research MIT code with AIMD Labs changes;
the emulation clock was authored by AIMD Labs agents (revisions e5a45ca, 012db28, 78892f2).
The ported modules contain only Nous MIT and AIMD-authored code.
Reader structures, numeric address profiles, symbolic constants and the text decoder were
informed by pret/pokefirered at revision `037335f4c725d7c9aecdac87066f2002b4bd7e14`.
These are technical format descriptions and parser code; this tree omits the copied map-label
index and all distributed extracted game datasets. Retained profiles carry per-field source notes.
The numeric map-index recipe follows `tools/mapjson/mapjson.cpp:436-456` at that same pret
revision: adjacent pointer arrays precede their aligned root. See `data/maps.s:11-17` and
`map_data_rules.mk:30-31` for the build path. Only the structural rule is used; no map labels,
per-group count dataset or upstream source dump is bundled.
A public decompilation repository is not a license grant for game content.

The local setup extractor uses that same pret commit at user runtime, with canonical content-tree
SHA-256 `df2f96d640e48ccf62db35d14519f4efd798773f0e9352965b168a845bea526e`.
Encoding and offline rules are in `docs/local-data.md`. The ordered-map and tileset INCBIN/layout
recipes follow AIMD Labs `worldgen.py` and `regiongen.py` from historical source
`7185ae105ca0317753f53e0f16eb1843028df119` (MIT fork retaining Nous Research notices).
Only parser recipes and message-label/policy mappings are shipped; no historic generated tables,
old Git history, upstream text, ROM or decompilation source is bundled. The extractors do not
execute source scripts or interpret a general scripting language.

The System One wire protocol was studied at wfzyx/von revision
`56e845299f573af06480db6035f5d13f0f3baf51`; the client implementation is original.
No ROM, emulator binary, external skills or historical asset payload is included.
See THIRD_PARTY_NOTICES.md for limitations and dependency notices.
