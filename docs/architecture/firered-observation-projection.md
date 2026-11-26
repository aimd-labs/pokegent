# FireRed observation projection

`pokegent/games/gen3/firered/observation.py` owns `project_observation`, the opt-in
projection of a validated FireRed `StateSnapshot` onto the generic observation
envelope (#139, R10). It is Python-only internal SDK data: not a provider payload,
not a redaction claim and not a public-exposure guarantee. It adds no app wiring,
CLI flag, discovery, input legality, ROM-digest admission, native or second-title
proof. The snapshot's `rom_profile` label is copied; this module does not
independently verify identity.

## Callable

```python
project_observation(
    snapshot: StateSnapshot,
    *,
    restore_epoch: int,
    enabled: bool = False,
) -> GameObservation
```

`enabled` defaults to `False` and is checked first with `type(enabled) is bool`.
A non-bool raises `ObservationRejected` before any snapshot attribute is read; the
default `False` then refuses rather than returning an unusable observation. Next,
`snapshot` must be an actual `StateSnapshot` instance, not a duck-typed or partial
object. Only after both checks are snapshot fields read.

## Projected result

The result is the ordinary frozen `GameObservation` built through
`create_observation(enabled=True, ...)`; generic metadata, counter and freezing
rules are unchanged. `game_id` is `"firered"`, `profile_id` is
`snapshot.rom_profile`, `snapshot_seq` is `snapshot.snapshot_seq`, and
`restore_epoch` is the caller's keyword argument.

`usable` is exactly `snapshot.usable`, which is `snapshot.health.valid`. It does
not consult additional health confidence, `controls_locked`, read-error detail,
game legality or action policy. A usable snapshot contributes exactly four
primitive payload keys from `snapshot.player`: `map_group`, `map_num`, `x` and
`y`, and nothing else. An unusable snapshot contributes an empty payload `{}`;
the projection never reads a partial player or attempts to repair unknown state.

## Restore epoch

`restore_epoch` is a required caller assertion, validated only by the generic
non-negative plain-int counter rule. The projection does not infer, derive, track,
compare or persist restore authority or epoch history. Generic
`validate_transition` is separately owned; `project_observation` does not call it.
A caller that needs epoch or sequence succession checks must call that helper
explicitly between observations.

## Plugin exposure

`FIRERED_CAPABILITIES` includes `observation_projection`. `FireRedServices` adds
`observation_factory: Callable[..., Any] | None = None`, so manual construction
stays valid with `None`; `compose()` names `project_observation` there without
invoking it or reading an actor or ROM. This names the callable only and adds no
runner injection, required capability, CLI flag or discovery.

## Boundary

The projector only checks that the caller supplied a `StateSnapshot` instance; it
does not call `StateSnapshot.create` or revalidate that instance. Callers must
supply a normally validated snapshot: the projector does not repair or revalidate
instances built via bypass. The fence below uses public `StateSnapshot.create`
with an invented profile label, name and coordinates, no ROM bytes, and it
neither admits a ROM nor starts an actor. It is source-level evidence, not native,
game, provider or release proof. [#139] stays open.

## Synthetic fence

Run once with CPython 3.11, worktree `PYTHONPATH` and the shared heavy lock;
expected stdout is the printed line and stderr is empty.

```python
"""Synthetic fence: FireRed observation projection (#139/R10)."""

from pokegent.games.gen3.firered.observation import project_observation
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet, Health, Player, ReadError, StateSnapshot, VarTable
)
from pokegent.games.observation import ObservationRejected

PROFILE = "invented-firered-profile"  # label only; no ROM bytes or digest


def invented_player() -> Player:
    return Player(
        name="Guide", gender="male", tid=0, sid=0, map_group=3, map_num=7,
        map_layout_id=0, x=10, y=11, money=0, coins=0,
    )


usable = StateSnapshot.create(
    rom_profile=PROFILE,
    snapshot_seq=5,
    health=Health(valid=True),
    player=invented_player(),
    flags=FlagSet(),
    vars=VarTable(),
)

error = ReadError(subsystem="synthetic", code="invalid", message="synthetic")
unusable = StateSnapshot.create(
    rom_profile=PROFILE,
    snapshot_seq=6,
    health=Health(valid=False, read_errors=(error,)),
)

try:
    project_observation(usable, restore_epoch=2)
except ObservationRejected:
    pass
else:
    raise AssertionError("default enabled=False was not refused")

usable_observation = project_observation(usable, restore_epoch=2, enabled=True)
assert usable_observation.usable is True
assert (usable_observation.snapshot_seq, usable_observation.restore_epoch) == (5, 2)
assert dict(usable_observation.payload) == {"map_group": 3, "map_num": 7, "x": 10, "y": 11}
assert usable_observation.require_usable() is usable_observation

unusable_observation = project_observation(unusable, restore_epoch=2, enabled=True)
assert unusable_observation.usable is False
assert dict(unusable_observation.payload) == {}
try:
    unusable_observation.require_usable()
except ObservationRejected:
    pass
else:
    raise AssertionError("unusable observation was accepted as usable")

print("observation projection fence ok: opt-in refusal, four fields, empty unknown payload")
```

[#139]: https://github.com/aimd-labs/pokegent/issues/139
