# R10 — Proposed GamePlugin and backend contract

Status: historical proposal written 2026-11-24 for maintainer review, not frozen. Runtime baseline c73774285e5e164d01629ca14a5e8e551e03493f. Implements research scope of [#139](https://github.com/aimd-labs/pokegent/issues/139), not issue closure. R04, R05, R08 and R09 are inputs; this contract owns generic admission, and R08 only supplies DS evidence. Current v1.x contracts are authoritative. That proposal made no runtime API change, profile promotion, backend selection, ROM or test execution. The current dated implementation status is below.

Implementation status (2026-11-26, source base `95197354`): the reviewed
[plugin registry](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/games/plugin.py)
is disabled by default and admits only explicit reviewed plugins against exact SHA-1 ROM bytes,
with profile, contract-version, capability and duplicate-ownership refusal
([#246](https://github.com/aimd-labs/pokegent/pull/246)). The
[observation envelope](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/games/observation.py)
provides internal, immutable flat projections and is explicitly not a redaction or provider
payload ([#250](https://github.com/aimd-labs/pokegent/pull/250)). The
[choice SDK](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/sdk.py)
validates a caller-supplied frame and result locally and never reads a snapshot or sends input
([#253](https://github.com/aimd-labs/pokegent/pull/253)).

The opt-in composition APIs are Python-only, with no CLI flags. In
[`build_runner`](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/app.py#L657-L696),
`experimental_game_plugin=True` requires caller-supplied `plugin_rom_bytes`; the built-in
[FireRed plugin](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/games/gen3/firered/plugin.py)
must admit those exact bytes before any actor-backed read or reader construction, and its
[composed services](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/games/gen3/firered/plugin.py#L87-L104)
now supply the existing adapter and composite source together with menu, family-registration and
objective-observation factories
([#271](https://github.com/aimd-labs/pokegent/pull/271),
[#273](https://github.com/aimd-labs/pokegent/pull/273)). The
[objective helper](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/games/gen3/firered/admission.py#L23-L52)
creates one `LiveReader` per `build_runner` call only when both the actor and profile are present;
without either it returns two `None` providers and constructs no reader. Each admission callback
reads once from that reader, never a cached decision snapshot. Separately,
[`open_emulator`](https://github.com/aimd-labs/pokegent/blob/9519735445fece6c609cff727adbbd901d65d694/pokegent/app.py#L1308-L1361)
accepts `experimental_backend=True` with a trusted `backend_factory`: the callable is checked
before use, called only after ROM profile admission, and must return an `Emulator`; the actor
then owns the backend lifecycle
([#268](https://github.com/aimd-labs/pokegent/pull/268)). Both switches default to `False`, and
non-bool values are rejected.

Current selection remains FireRed-only (`BPRE` version 0), and validators, targets and probes
remain game-specific. These slices do not establish a generic second-title path, plugin
discovery or a native compatibility promise.
[#139](https://github.com/aimd-labs/pokegent/issues/139) remains open.

## Evidence

| Reference | Exact pin and inspected surfaces | Boundary |
|---|---|---|
| mGBA | [431935616c0e216d650b25b2ea0c0acccd359038](https://github.com/mgba-emu/mgba/tree/431935616c0e216d650b25b2ea0c0acccd359038); [core API](https://github.com/mgba-emu/mgba/blob/431935616c0e216d650b25b2ea0c0acccd359038/include/mgba/core/core.h), [Python core](https://github.com/mgba-emu/mgba/blob/431935616c0e216d650b25b2ea0c0acccd359038/src/platform/python/mgba/core.py) | MPL-2.0 metadata; binding minimum tested 0.10.2 within the declared `mgba>=0.10.2` range, not a separate exact pin; latest source is not current native compatibility |
| PyBoy | [350e96fff43ae8bff076c0f90a364b410cb4b930](https://github.com/Baekalfen/PyBoy/tree/350e96fff43ae8bff076c0f90a364b410cb4b930); [memory and API](https://github.com/Baekalfen/PyBoy/blob/350e96fff43ae8bff076c0f90a364b410cb4b930/pyboy/pyboy.py), [LGPL v3 text](https://github.com/Baekalfen/PyBoy/blob/350e96fff43ae8bff076c0f90a364b410cb4b930/LICENSE.md) | bank-aware memory access, input and state; no automatic adoption of latest build |
| Azahar | [4a147c509571a3d16a06abe679fe1339dbbdbfd7](https://github.com/azahar-emu/azahar/tree/4a147c509571a3d16a06abe679fe1339dbbdbfd7); [System](https://github.com/azahar-emu/azahar/blob/4a147c509571a3d16a06abe679fe1339dbbdbfd7/src/core/core.h), [savestate](https://github.com/azahar-emu/azahar/blob/4a147c509571a3d16a06abe679fe1339dbbdbfd7/src/core/savestate.h), [LICENSE.txt](https://github.com/azahar-emu/azahar/blob/4a147c509571a3d16a06abe679fe1339dbbdbfd7/LICENSE.txt) | GPL-3.0 root text; legacy per-file headers mix GPLv3-or-later and GPLv2-or-later, so no clearance or adoption follows; explicit build mismatch error; no public stable Python/headless integration established |
| PokemonAutomation | [1b7089e293bd17878ae44e4442f22435555f7cc1](https://github.com/PokemonAutomation/Arduino-Source/tree/1b7089e293bd17878ae44e4442f22435555f7cc1); [video](https://github.com/PokemonAutomation/Arduino-Source/blob/1b7089e293bd17878ae44e4442f22435555f7cc1/SerialPrograms/Source/CommonFramework/Tools/VideoStream.h), [controller scheduler](https://github.com/PokemonAutomation/Arduino-Source/blob/1b7089e293bd17878ae44e4442f22435555f7cc1/SerialPrograms/Source/NintendoSwitch/Controllers/NintendoSwitch_ControllerWithScheduler.h), [README](https://github.com/PokemonAutomation/Arduino-Source/blob/1b7089e293bd17878ae44e4442f22435555f7cc1/README.md) | MIT source generally, per-file exceptions and noncommercial precompiled binaries explicitly noted; visual/controller reference only |

R08 pins melonDS/BizHawk; this contract owns generic admission and uses R08 only as DS evidence, not as a competing gate. All four trees nontruncated; selected source only. No code adoption, dependency license clearance or packaging decision.

## Existing seams and blockers

| Current source | Required change / characterizing proof |
|---|---|
| [open_emulator](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/app.py#L1278-L1307) hardcodes FireRed admission and mGBA | inject exact reviewed game/backend selection; retain Rev0 unchanged |
| [build_runner](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/app.py#L650-L856) constructs FireRed menu, tables, readers and registration | adapter supplies composed components; no new title branches in generic runner |
| [FireRedAdapter](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/games/gen3/firered/reader/adapter.py) bundles reader/rules, registration no-op | fill seam only after characterization; one registry and unique scene owner |
| [Perceiver](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/pipeline.py), [BuilderRegistry](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/options/registry.py), [ExecutorSpec/VerifyContext](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/actions/_exec/registry.py) import FireRed StateSnapshot | generic observation envelope and game-owned projections; preserve frozen options/result contracts |
| [objective validation](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/validate.py), [tracker](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/goals/tracker.py), [navigation models](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/navigation/models.py) | adapter-resolved targets/probes and namespaced location IDs, no forced GBA map group/number everywhere |
| [actor](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/emulator.py#L1286-L1348), [save/load](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/pokegent/emulator.py#L1617-L1656) | parameterized memory domains, declared state bytes/input/clock; test actor ownership, no raw bypass |
| [actor tests](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/emulator/test_emulator_actor.py), [input tests](https://github.com/aimd-labs/pokegent/blob/c73774285e5e164d01629ca14a5e8e551e03493f/tests/emulator/test_emulator_input.py) | preserve immutable snapshot/overlap/invalid input/chord assertions; parametrize contract suite |

## Proposed interface sketch (not source code)

Keep GamePlugin distinct from backend and optional policy extension. Built-in reviewed adapters selected by explicit registry and admitted ROM; no arbitrary module import from config or unreviewed third-party plugin loader. One immutable descriptor per exact title/profile and compatible contract version.

| Operation | Input → Output | Invariants |
|---|---|---|
| identify | private ROM artifact + metadata → candidate identity | exact digest, title, revision, language; unknown/refuse, not suffix guess |
| admit | identity + profile + backend/local-data receipts → admitted descriptor | all applicable required capabilities/provenance; no side effects until admitted |
| compose | admitted descriptor + read-only snapshot port + reviewed registries → game services | bounded decoder, scene/option/executor/verifier ownership, local rules and objective validators; no model-specific logic |
| observe | atomic backend observation → game snapshot | typed usable/unknown/inapplicable, game/profile/frame/epoch identity, explicit evidence; private raw fields not provider payload |
| build | current snapshot + scene + validated objective → registered legal options | no unsupported/unknown executable options; exact ownership, risk, pre/postconditions |
| verify | before/after snapshots + registered action → existing ActionResult | independent effect proof, game outcome distinct, durable progress only with evidence |
| resolve | game-local objective target/probe → resolved typed target + checkable predicate | no global map assumption; CAS remains objective session authority |
| restore_validate | admitted identity + private checkpoint receipt → accepted current snapshot or refusal | no old frame replay; exact backend/profile/format and restore epoch |

No universal Pokemon struct. Shared required fields minimal: identity, snapshot sequence and restore epoch, usable status, evidence/redaction policy, generic scene family, stable option ID. Game-specific typed views carry applicable stats, map, battle, party, quest and generation rules. Option wire schema remains current; changes additive/versioned only after ADR. Capability declarations must separate implemented from independently verified, include unsupported and unknown; no "all games" default.

## Backend capability groups

- B0 lifecycle/identity: exact admitted ROM/profile, backend version/build and configuration, close/stop, private read-only ROM ownership.
- B1 controlled input: digital buttons, optional touch/analog, normalized supported controls, hold/release/advance under bounded single writer, cleanup on disconnect/timeout, no arbitrary input API exposed to agents.
- B2 observation: atomic paused snapshots across defined memory regions/banks/domains, frame seq/epoch, immutable bytes, optional screenshot/audio with same-frame or explicit freshness; no read/write pass-through to agent.
- B3 checkpoint: bounded in-memory state format if backend supports it; atomic file receipt via actor, exact identity and format, validated restore, epoch change. Hardware without state restore reports unsupported rather than faking checkpoint.
- B4 timing: deterministic frame stepping where available; hardware/real-time only observations have explicit wall-time/latency and cannot claim deterministic frame control. Separate observation confidence from legal semantic proof. Vision-only may not meet current RAM-first invariant; remains experimental and no input authority until an approved separate evidence contract.

Process-separated bridge candidate: authenticated/controlled local transport, versioned handshake (backend capability/identity), monotonic request/sequence/epoch, one control lease, bounded requests and response sizes, explicit cancellation and idempotent observation only. Never retry an input blindly after ambiguous delivery; reobserve current state and report uncertain outcome. No shell/script/RAM-write tool; failure stops or safely pauses. No mandatory HTTP, Lua, Node or desktop UI. SDK/MCP facade R04 stays above execution, only exposes offered options, objective CAS and evidence.

## Incremental implementation slices (all proposed)

1. Characterize baseline Rev0: import inventory, exact golden frames/results, existing tests + private native smoke. No behavioral change.
2. Introduce reviewed selection/descriptor seam and injectable backend; fake adapter only for contract exercise, no native support claims. Extract snapshot projection from FireRed reader types in core without changing v1.x external schema.
3. Move composition and rule/scene/catalog registration to FireRed plugin; exact single ownership, builder+executor+verifier, no fallback to raw keys; tests prove same behavior.
4. Move game-specific map/target/probe/knowledge interfaces behind plugin; retain objective CAS and R05 data provenance.
5. Candidate second title: FireRed Rev1/LeafGreen per #139, verify independently; then Emerald to prove distinct world/rules rather than only different addresses. Do not admit by toggling supported flag. Preserve R02/R03 issues and paused #106.
6. PyBoy actor adaptation and exact Red profile (R04); follow Gen1/2; DS via R08; 3DS/Switch hardware/native port research only. No promise of later-generation full functionality.

## Acceptance matrix

| Case | Acceptance |
|---|---|
| G01 unknown/mismatched profile | before input, typed refusal, no fallback title |
| G02 unsupported capabilities | explicit unsupported/unknown, no placeholder success |
| G03 one writer and immutable snapshot | stale/repeated frame, concurrent reader, interrupted command never causes duplicate inputs |
| G04 backend conformance | region/bank bounds, frame and input behavior, release, state bytes/hash and restore epoch |
| G05 registration | duplicate scene/action/verifier and missing owner fail deterministically |
| G06 no provider leakage | no raw executor refs/args, hidden game data or private state |
| G07 objectives | exact title-local target/probe; newer revision protected; unknown does not complete |
| G08 second-title vertical slice | exact admitted profile, readable state, ready scene, verified navigation, battle transition and restore; no new core gameplay branches |
| G09 all three modes | same legal-action frame and executor, mode-specific ownership unchanged |
| G10 FireRed regression | full existing public checks and private reviewed smoke + native scenario receipts |
| G11 unavailable transport | bounded pause/no blind input retry; disconnect releases where supported |
| G12 unsupported visual/hardware | no native/RAM proof claim; explicit limited observation capabilities |

R09 supplies manifests and scenario/campaign evidence, R12 release gate. This is a contract proposal requiring ADR and senior review, not implementation authorization or support upgrade. No tests run.

## Explicit caller registry injection (`build_runner`)

Later implementation status (2026-11-26, source base
`d83e823a6dc19e1e496a6d10d1b015293c7d9eb1`): `build_runner` accepts an optional
`plugin_registry` ([#284](https://github.com/aimd-labs/pokegent/pull/284)). Python-API-only:
no flag, loader, discovery or entry point was added.

- The opt-in stays explicit: only with `experimental_game_plugin=True` is `plugin_registry`
  accepted; otherwise the call refuses with `RefusalCode.DISABLED` before ROM bytes are
  required, so an orphan registry cannot fall back to the built-in path. A value must be a
  `ReviewedPluginRegistry`; other objects raise `TypeError`. The caller still owns the bytes:
  `plugin_rom_bytes` stays required, with no path, discovery or filesystem read implied.
- A supplied registry is used as-is for one `firered` selection and only that instance. It is
  never copied, merged, registered into the built-in registry or replaced by it; an empty,
  disabled or non-matching caller registry refuses instead.
- The existing FireRed profile gate remains mandatory: `FireRedPlugin(profile)` is constructed
  before selection even with a caller registry, so `supported`, `BPRE` and ROM version 0 are
  still checked.
- Required capabilities are derived by the current selection path, not a frozen catalogue:
  `snapshot_source`, `menu_services`, `family_registration` and `objective_observation` are
  always required, plus `dex_fallback` and `story_fallback` only when their config flags are
  set. Everything is checked before `compose()`.
- Selection always requests the `firered` namespace, so a plugin registered under another game
  name is refused with `RefusalCode.PROFILE_MISMATCH` before compose. Unknown digests,
  unsupported contracts, profile mismatches and missing capabilities also refuse before
  compose and before any actor read.
- `plugin_registry=None` is unchanged: the opt-in still builds a fresh enabled registry and
  registers the built-in `FireRedPlugin`; the opt-in-off path with no inputs is untouched.

Boundary: FireRed-only composition and source proof in synthetic
[`tests/test_runner_plugin_registry.py`](https://github.com/aimd-labs/pokegent/blob/d83e823a6dc19e1e496a6d10d1b015293c7d9eb1/tests/test_runner_plugin_registry.py);
it does not establish a second game, discovery, native support, input authority or action
verification. The fence exercises the disabled registry only, proving refusal before
`compose()` and any actor attribute, not admitted wiring.

```python
"""Disabled caller registry: refuse before compose or any actor read."""
import hashlib
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import PluginRefusal, RefusalCode, ReviewedPluginRegistry

ROM_BYTES = b"docs-runner-registry-invented-bytes" * 8  # invented, not a ROM
base = load_profiles().get("firered-us-rev0")
PROFILE = base.model_copy(
    update={"rom": base.rom.model_copy(update={"sha1": hashlib.sha1(ROM_BYTES).hexdigest()})}
)

class NoReadActor:
    realtime_idle_while_deciding = False

    def __getattr__(self, name):
        raise AssertionError(f"actor.{name} touched before refusal")

class ComposeSpy:
    def __init__(self):
        self.composes, self.descriptor = 0, FireRedPlugin(PROFILE).descriptor

    def compose(self):
        self.composes += 1

plugin = ComposeSpy()
registry = ReviewedPluginRegistry(enabled=False)
registry.register(plugin)

try:
    build_runner(
        actor=NoReadActor(),
        profile=PROFILE,
        events=object(),
        config=AppConfig(run_id="runner-registry-guide", map_mode="always"),
        experimental_game_plugin=True,
        plugin_rom_bytes=ROM_BYTES,
        plugin_registry=registry,
    )
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED, refusal.code
else:
    raise AssertionError("disabled caller registry was admitted")

assert plugin.composes == 0
print("refused before compose:", RefusalCode.DISABLED.value)
```
