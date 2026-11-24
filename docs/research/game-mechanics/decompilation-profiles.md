# R05 — Game profile and decompilation research

Status: proposed, selected source inspection, 2026-11-24. Baseline [c73774285e5e164d01629ca14a5e8e551e03493f](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/README.md). Follow [#139](https://github.com/aimd-labs/pokegent/issues/139), R04 and R10. No ROM, extracted data, game assets, source archive, or executable code added. Public availability of a decompilation does not grant a license to its game content. All reference sources below are REFERENCE, not adoption approval.

## Pinned evidence

| Project | Source pin | Build/layout evidence inspected |
|---|---|---|
| pret/pokefirered | [037335f4c725d7c9aecdac87066f2002b4bd7e14](https://github.com/pret/pokefirered/tree/037335f4c725d7c9aecdac87066f2002b4bd7e14) | [README](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/README.md); [Pokémon](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/include/pokemon.h); [save layout](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/include/global.h) |
| pret/pokeruby | [5784633ce4ef7ade1a7f2d2d0c288e3d5e6cdd7f](https://github.com/pret/pokeruby/tree/5784633ce4ef7ade1a7f2d2d0c288e3d5e6cdd7f) | [README](https://github.com/pret/pokeruby/blob/5784633ce4ef7ade1a7f2d2d0c288e3d5e6cdd7f/README.md); [Pokémon](https://github.com/pret/pokeruby/blob/5784633ce4ef7ade1a7f2d2d0c288e3d5e6cdd7f/include/pokemon.h); [save layout](https://github.com/pret/pokeruby/blob/5784633ce4ef7ade1a7f2d2d0c288e3d5e6cdd7f/include/global.h) |
| pret/pokeemerald | [8cf518839e529eeb2477905ede1df7213ac134b8](https://github.com/pret/pokeemerald/tree/8cf518839e529eeb2477905ede1df7213ac134b8) | [README](https://github.com/pret/pokeemerald/blob/8cf518839e529eeb2477905ede1df7213ac134b8/README.md); [Pokémon](https://github.com/pret/pokeemerald/blob/8cf518839e529eeb2477905ede1df7213ac134b8/include/pokemon.h); [save layout](https://github.com/pret/pokeemerald/blob/8cf518839e529eeb2477905ede1df7213ac134b8/include/global.h) |
| pret/pokered | [ba9c0568f64ae602fadda577a08c66639f93082a](https://github.com/pret/pokered/tree/ba9c0568f64ae602fadda577a08c66639f93082a) | [README](https://github.com/pret/pokered/blob/ba9c0568f64ae602fadda577a08c66639f93082a/README.md); [WRAM](https://github.com/pret/pokered/blob/ba9c0568f64ae602fadda577a08c66639f93082a/ram/wram.asm) |
| pret/pokecrystal | [9ce4c4e5068ffdf56e23ca3d8dc9178b2465d482](https://github.com/pret/pokecrystal/tree/9ce4c4e5068ffdf56e23ca3d8dc9178b2465d482) | [README](https://github.com/pret/pokecrystal/blob/9ce4c4e5068ffdf56e23ca3d8dc9178b2465d482/README.md); [WRAM](https://github.com/pret/pokecrystal/blob/9ce4c4e5068ffdf56e23ca3d8dc9178b2465d482/ram/wram.asm) |

All five recursive trees were inspected without truncation; selected README/struct/WRAM declarations read. DS, later titles, other pret projects and Holon remain candidates, not audited in this slice. No builds performed or matching ROMs obtained. Preserve existing FireRed source-lock pin and verified content digest.

## Key findings

- FireRed and LeafGreen share one source project with separate declared English Rev0/Rev1 and Switch build identities. [Rev1 checksum](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/firered_rev1.sha1) and [LeafGreen checksum](https://github.com/pret/pokefirered/blob/037335f4c725d7c9aecdac87066f2002b4bd7e14/leafgreen.sha1) are reference identity evidence, not admission. Current [profile catalog](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/references/rom_profiles.yaml) admits only FireRed US Rev0 and explicitly refuses Rev1.
- FRLG and Emerald have relocatable save pointers and structurally similar encrypted Pokémon data. SaveBlock1 party location and SaveBlock2 encryption-key location differ (source annotations in global.h). Ruby declares fixed save block objects; do not assume Emerald or FRLG pointer/key layout applies. Gen3 common mechanics and per-ROM addresses/struct layouts must be separated.
- Red and Crystal use RGBDS-style WRAM declarations rather than GBA EWRAM/IWRAM; Crystal adds banked WRAM, different map/script/battle state. Gen1 party uses internal species IDs and one Special stat; Gen2 requires separate type/stat/item/clock and storage semantics. Do not extend GBA SymbolEntry regions to fake Game Boy addresses. Build candidate profiles from exact title/revision/language/mapper, not extensions.
- Existing [RomProfile](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/firered/rom/rom_data.py) explicitly encodes GBA regions and bounded fields; [local-data setup](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/docs/local-data.md) uses pinned archive and tree digest, generates seven private files atomically, validates cache, refuses conflicting output. Keep this workflow; don't run downloaded source scripts or publish generated tables.

## Proposed extraction and admission pipeline

1. Select exact supported candidate using reference hash, header, size and independently verified user's local ROM. Unsupported/mismatched/ambiguous identity refuses before input. Hash provenance alone is not runtime evidence.
2. Pin reviewed technical source, complete canonical source content digest and generator version. For new platforms, use bounded parsers of declared formats; no arbitrary macro/script evaluation, shell execution, symlinks/traversal, external asset download or silent source substitution. A locally produced symbol map may be an explicitly supplied private artifact, not a mandatory untrusted build.
3. Derive separately: profile (numeric interoperable symbols/region/layout and per-field source), local tables (private), maps/edges/gates (private topology and current RAM facts), game rules (independent implementation), scene signatures and option registration. Track confidence/unknown independently of support.
4. Validate profile structure, addresses, read sizes, pointer bounds, bitfields, overlay regions and game-specific decoder invariants. Compare known synthetic vectors and local native snapshots. Require exact profile provenance for every symbol; unresolved data must stay unknown.
5. Publish private manifest with game/hash/source/generator/schema, artifact hashes/sizes, compatibility and validation receipts; atomic output to absent directory, corruption refuses, identical setup idempotent. Cache key includes title/revision/ROM digest + source identity + extractor/schema; no cross-game cache reuse. Keep old FireRed behavior unchanged.
6. Admit a candidate only after reviewed backend conformance, local setup, reader/scene tests, verified field movement and battle/restore scenarios. Full story and ordinary mechanics require independent subsequent tests. No automatic change to supported flag.

## Candidate profile sequence

| Candidate | Research state | Primary proof |
|---|---|---|
| FireRed US Rev0 | existing admitted; reference pin preserved | regression against current native/synthetic contracts |
| FireRed US Rev1 | identified, explicitly unsupported | separate symbols/menus/flags and local setup; no copied Rev0 addresses |
| LeafGreen English Rev0 then Rev1 | reference checksum declared | exact variant, exclusives and game-specific progression |
| Emerald | reference declared | Hoenn topology, save layout, callbacks, dual battle/field differences |
| Ruby/Sapphire | reference declarations | fixed save blocks, title/revision rules, regional encounter differences |
| Red/Blue | source declaration | GB backend conformance, exact identity, typed GB reader |
| Crystal | source declaration | banked WRAM, RTC and Gen2 semantics |
| DS+ | deferred to R08/R10 | identity, overlays, dual screens, asynchronous scene and backend proof |

## Acceptance and existing coordination

- Synthetic negative cases: wrong hash/header/size, unsupported revision, out-of-range/banked address, truncated source, ambiguous symbol, inconsistent layout, mismatched cache, stale state after restore, unknown field. No model input or game progress on refusal.
- Private cases: cold boot, intro, current party/bag/map/flags, legal walk, battle transition, state round-trip, exact destination, failure/unknown. R09 records manifests; R10 owns plugin/backend seam. [#143](https://github.com/aimd-labs/pokegent/issues/143) publishes only proven compatibility; [#144](https://github.com/aimd-labs/pokegent/issues/144) reproducibility; existing [public data guard](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tools/check_rom_data.py) remains mandatory.
- No runtime implementation, license clearance, native tests, build, promotion or profile change in this proposal. Full per-revision symbol audit and all later generations are explicitly outstanding.
