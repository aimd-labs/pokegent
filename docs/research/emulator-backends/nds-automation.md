# R08 — Nintendo DS automation and game adapter research

Status: proposed, 2026-11-24. Runtime baseline c73774285e5e164d01629ca14a5e8e551e03493f. R10 owns generic admission; this package only supplies DS evidence. #139 remains the implementation owner. No DS ROM, native test, emulator build, Lua execution, data copy or backend commitment.

## Pinned evidence

- [wyanido/pokebot-nds @ d73aee2bb96ca19384f9d47b5a4554ca248ee3ce](https://github.com/wyanido/pokebot-nds/tree/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce)
- [melonDS-emu/melonDS @ ff972523f8a70e877dd054439876c3c8c6b79197](https://github.com/melonDS-emu/melonDS/tree/ff972523f8a70e877dd054439876c3c8c6b79197)
- [TASEmulators/BizHawk @ 05259649a55cd790ceeb9451344ef08db40c1080](https://github.com/TASEmulators/BizHawk/tree/05259649a55cd790ceeb9451344ef08db40c1080)
- [pret/pokeplatinum @ 485c400f9d7bad10d906d556ba23e5e46d5b3e5e](https://github.com/pret/pokeplatinum/tree/485c400f9d7bad10d906d556ba23e5e46d5b3e5e)
- [pret/pokeheartgold @ 8946577385ef0e3b1a29f2f56c03db20fd675cb0](https://github.com/pret/pokeheartgold/tree/8946577385ef0e3b1a29f2f56c03db20fd675cb0)
- [Project-Holon/aletheia @ 50731dd699fb0fb3f881cd028a67d8f6aa649a88](https://github.com/Project-Holon/aletheia/tree/50731dd699fb0fb3f881cd028a67d8f6aa649a88)

PokéBot NDS is archived (GitHub metadata); its [README](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/README.md) claims all languages and DPPt, HGSS, BW, B2W2 support for selected automation modes, but the same README limits Gen 4 to English with minimal non-English support. No broad language or full-title support follows from that text. [MIT root license](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/LICENSE) reviewed; data tables/assets not automatically cleared. melonDS metadata GPL-3.0; source headers say v3 or later; keep separate reference/backend candidate, no bundled core. BizHawk metadata NOASSERTION: mixed cores/components require per-component review. pret projects no recognized license signal; technical references only. Holon tree response truncated, so only its pin is registered as candidate, not audited. None establishes a native Pokegent DS implementation.

## Source audit

- [detect_emu.lua](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/detect_emu.lua) normalizes BizHawk and DeSmuME reads based on emulator-specific APIs. [detect_game.lua](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/detect_game.lua) maps game code/language to offset and family method overrides. This is evidence of platform and title distinctions, NOT an exact revision or hash admission proof. Don't copy offsets or assume one language shift applies to every symbol.
- [Gen IV](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/methods/gen_iv.lua) and [Gen V](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/methods/gen_v.lua) have different pointers, battle indicators/menu state, fishing conditions and input routines; Gen V uses touch interactions in menus. Same ordinary capability, distinct owner/decoder/executor by exact title. [Pokémon decoder](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/modules/pokemon.lua) decrypts/reorders blocks with checksum-based validation, handles exceptions for raw encounters; do not trust unvalidated decoded fields or use Gen3 PID/XOR layout. Reimplement via reviewed format facts and synthetic vectors.
- [Input](https://github.com/wyanido/pokebot-nds/blob/d73aee2bb96ca19384f9d47b5a4554ca248ee3ce/lua/modules/input.lua) shows separate stylus/analog coordinate APIs, held directions and input release. A touch operation must be semantic and scoped to a verified bottom-screen control, with native coordinates, down/up and bounded frame acknowledgment. Never assume scaled dashboard pixels map directly to touch. Avoid soft-reset, randomization and auto-release scripts in normal play.
- [melonDS NDS interface](https://github.com/melonDS-emu/melonDS/blob/ff972523f8a70e877dd054439876c3c8c6b79197/src/NDS.h) has ARM9/ARM7 and shared memory and state methods. [Savestate](https://github.com/melonDS-emu/melonDS/blob/ff972523f8a70e877dd054439876c3c8c6b79197/src/Savestate.h) has explicit format versions. A core method is not proof of a supported Python binding/headless public API. Need independently prove control, build, snapshot, restore and firmware/config identity.
- [BizHawk memory declarations](https://github.com/TASEmulators/BizHawk/blob/05259649a55cd790ceeb9451344ef08db40c1080/Assets/Lua/_docs_luacats/memory.d.lua), [joypad](https://github.com/TASEmulators/BizHawk/blob/05259649a55cd790ceeb9451344ef08db40c1080/Assets/Lua/_docs_luacats/joypad.d.lua), [savestate](https://github.com/TASEmulators/BizHawk/blob/05259649a55cd790ceeb9451344ef08db40c1080/Assets/Lua/_docs_luacats/savestate.d.lua) show memory-domain selection, digital/analog control and state operations. Declaration files aren't executable Lua; [NDS core wrapper](https://github.com/TASEmulators/BizHawk/blob/05259649a55cd790ceeb9451344ef08db40c1080/src/BizHawk.Emulation.Cores/Consoles/Nintendo/NDS/MelonDS.cs) integrates melonDS. A desktop Lua bridge is a reasonable research candidate, but verify actual version/domain/frame boundary, local transport and license; no mandatory UI/Node dependency in core.
- [Platinum README](https://github.com/pret/pokeplatinum/blob/485c400f9d7bad10d906d556ba23e5e46d5b3e5e/README.md) distinguishes US Rev0/Rev1; [field system](https://github.com/pret/pokeplatinum/blob/485c400f9d7bad10d906d556ba23e5e46d5b3e5e/include/field_system.h) references overlay/save context. [HGSS README](https://github.com/pret/pokeheartgold/blob/8946577385ef0e3b1a29f2f56c03db20fd675cb0/README.md) explicitly WIP and unstable symbol layout. Source pins and local symbols must match ROM; dynamic overlays/pointers must be validated at each scene. No broad "DS memory offsets" profile.

## Core vs policy

| Ordinary core mechanic | Title-specific adapter | Optional specialized policy |
|---|---|---|
| encounter/capture, fishing | DPPt/HGSS/BW/B2W2 task and scene ownership | repeated shiny fishing/reset hunting |
| eggs/breeding | nursery and hatch/party rules | target-shiny/IV repeat |
| headbutt, phenomenon, grotto | HGSS headbutt, BW phenomena, B2W2 Hidden Grotto | repetitive hunting routes |
| menus, bag, party, storage, battle | DS dual-screen UI, touch/keys, per-title battle data | challenge/speedrun rules |
| progression, exploration, dex/team | exact map/overlay, scripts, flags, resources | no fixed walkthrough policy |

README mode claims do not prove full feature or campaign coverage. Starter/static/gift resets and filtering are specialized policies atop verified ordinary mechanics; never automatically reset or write RNG.

## Backend decision matrix (provisional)

| Candidate | Source evidence | Unknown / gate |
|---|---|---|
| BizHawk + local Lua bridge | memory domains, inputs, state, DS wrapper | native version/OS and deterministic snapshot/control test, bridge overhead, mixed licenses, no input races |
| melonDS separate service/native integration | core API and state format | supported integration and headless operation, build dependencies, distribution license, one writer, exact version |
| DeSmuME + Lua | PokéBot NDS adapter references | separate upstream API/source/license/host verification pending |

No scoring of performance or claim of a winner without running same scenarios. Prefer small backend capability proof, compare startup/p95 batch read+action latency and stable restore across same exact ROM and host, then choose. Keep process-separated backend possible, not compulsory.

## Proposed conformance and rollout

- Exact ROM + language/revision, backend/core/firmware configuration identity; private user-supplied legal ROM/firmware only. No automatic profile based on gamecode or extension. Unknown/mismatched profile fails before input.
- Backend snapshot includes memory domains (ARM9, ARM7, mapped/banked), dual display identities and monotonic logical frame + restore epoch. Read atomically at quiescent boundary, detect overlay transitions. Input map declares touch, shoulder and digital capabilities; exclusive actor, release on cancellation/timeout/disconnect; no raw arbitrary RAM write, reset, input or shell endpoint.
- State bytes/format/version/ROM receipt; restore must refuse incompatible state, invalidate previous frames, verify current reader before continuing. Read-only observation/screenshot separate from authority.
- Test N01 exact revision/unknown, N02 coherent multi-domain snapshot, N03 touch hit/release/stale target, N04 button release/frame bounds, N05 save restore epoch, N06 encrypted mon checksum and malformed input, N07 known/unknown battle/menu scene, N08 fishing three outcomes, N09 normal egg/hatch, N10 platform disconnect/no extra input. All proposed, not run.
- First prove one exact Platinum version after Gen3 seam and planned Gen1/2 work (existing #139 ordering); HGSS and BW/B2W2 separate adapter milestones. R05 source pipeline and R09 private native proof. Do not infer title support from boot, reference mode, or simulator.

Source coverage: selected Lua and emulator headers/declarations, README and tree inventory. No full audit of all 2,747 PokéBot files, complete DS reverse engineering, package license clearance, actual DS tests or profile promotion. #139 remains implementation owner; no issue changes.
