# FireRed battle facts factory

`pokegent/games/gen3/firered/battle.py` owns `make_battle_facts`, the game-owned entry point
for the battle family's mechanics facts (#139, R10). It is Python-only, FireRed-only and
source-level: nothing here adds a runner injection, required capability, CLI flag, title,
actor or native acceptance. Reviewed source pin `d6e60f1b` (PR #325 merged as `150ee085`).

## Callable and defaults

```python
make_battle_facts(
    profile: RomProfile,
    rom_path: str | os.PathLike[str] | None,
    kind: str,
    *,
    opponent_knowledge: str = "guide",
)
```

All three positional arguments are required; only `opponent_knowledge` is keyword-only with
a default. `profile` is read only for `profile.profile_id`, `rom_path` becomes the
`from_rom_file` path, and `opponent_knowledge` is forwarded unchanged. The factory chooses a
provider; it does not parse options, read config or add policy.

## Lazy returns

`kind == "basic"` or `rom_path is None` returns `None` before the mechanics import and before
any profile or path access. It is the `BasicFacts` answer the builder already understands, not
an error: no ROM, cache or profile lookup happens, and only a non-basic kind with a path reaches mechanics.

## Mechanics path

```python
MechanicsFacts.from_rom_file(
    rom_path,
    profile_id=profile.profile_id,
    opponent_knowledge=opponent_knowledge,
)
```

`MechanicsFacts` keeps its own defaults (`widen_unread_screens=True`,
`opponent_knowledge="guide"`) and its accepted production policy values are `"guide"` and
`"fair_play"`; any other value raises its `ValueError`. `from_rom_file` delegates to
`GameTables.from_rom_file`, which verifies and decodes the caller's ROM into the private
SHA-1-keyed table cache; the factory adds no second construction path, ROM read or fallback
provider.

## Exceptions

Factory construction errors are unchanged and propagate to the caller: cache, file and table
failures (`OSError`, `ProfileError`, `TablesError`) and the `ValueError` for an unsupported
knowledge policy are neither caught nor rewrapped here. Separately, the existing BattleBuilder
catches an exception from an assembled provider's method and falls back; that is builder
behavior, not this factory. `None` is reserved for basic and missing-path answers.

## App wrapper and plugin services

`pokegent.app.build_facts` keeps its signature and remains the app wrapper, delegating once to
`make_battle_facts` for the existing runner call site. `FireRedServices` adds
`battle_facts_factory: Callable[..., Any] | None = None`, so manual construction stays valid
with `None`; `compose()` names `make_battle_facts` there and advertises `battle_facts` without invoking it or reading an actor/ROM.

Plugin selection stays opt-in, and the field is optional rather than a new required
capability. The direct app factory was already the used path, so this split does not claim the
whole battle-facts behavior is newly default-off.

## Synthetic fence

Checked-in profile plus `compose()` exposure, then both lazy returns with a failing object.
No mechanics import, ROM read or monkeypatch: source wiring evidence, not native proof.

```python
"""Synthetic fence: FireRed battle facts factory exposure and lazy returns (#139)."""

from pokegent.games.gen3.firered.battle import make_battle_facts
from pokegent.games.gen3.firered.plugin import FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles


class Exploding:
    """Fails on any attribute or filesystem-path access."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected access: {name}")

    def __fspath__(self):
        raise AssertionError("unexpected ROM path access")


profile = load_profiles().get("firered-us-rev0")
assert profile is not None and profile.supported
services = FireRedPlugin(profile).compose()
assert services.battle_facts_factory is make_battle_facts
assert make_battle_facts(Exploding(), Exploding(), "basic") is None
assert make_battle_facts(Exploding(), None, "mechanics") is None
print("battle facts fence ok: composed factory, basic and missing path lazily returned None")
```

Run once with CPython 3.11 and worktree `PYTHONPATH`; expected stdout is the line above, stderr empty, no ROM bytes used.
