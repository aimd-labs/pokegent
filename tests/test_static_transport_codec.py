"""Focused synthetic tests for the unused transport static-index codec."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.rom import static_transport_codec as codec
from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.games.gen3.firered.rom.static_map_codec import MAX_CONTAINER_LENGTH
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError
from pokegent.navigation.graph import FerryRoute
from pokegent.navigation.models import NavigationNode


def node_row(node: NavigationNode) -> tuple[int, int, int, int, int]:
    return node.map_group, node.map_number, node.x, node.y, node.elevation


def ferry_row(route: FerryRoute) -> tuple:
    return (
        node_row(route.destination),
        route.guards,
        route.selector,
        route.local_id,
        route.coord,
        route.direction,
        route.menu_index,
        route.menu_id,
        route.menu_pc,
        route.menu_pcs,
    )


def route() -> FerryRoute:
    source = NavigationNode(3, 4, 5, 6, 1)
    return FerryRoute(
        source=source,
        destination=NavigationNode(-1, 0x22, 10, -11, 2),
        guards=(
            Guard("flag", 0x120, "eq", True, origin=(1, 2, "warp", 3)),
            Guard("var", 0x8000, "eq", 5, origin=(4, 5, "script", 6, 7)),
        ),
        selector=5,
        local_id=7,
        coord=(9, 10),
        direction="W",
        menu_index=2,
        menu_id=0xFF,
        menu_pc=0x800123,
        menu_pcs=((0, 0x800010), (1, 0x800020)),
    )


def facts() -> codec.StaticTransportFacts:
    base = route()
    second = replace(
        base,
        destination=NavigationNode(7, 8, -1, 0, 3),
        guards=(Guard("item", 13, "eq", 1),),
        selector=-4,
        local_id=None,
        coord=None,
        direction=None,
        menu_index=None,
        menu_id=None,
        menu_pc=None,
        menu_pcs=(),
    )
    return codec.StaticTransportFacts(
        elevators=(
            (
                (3, 4),
                (
                    (1, 2, 3, 5, (3, 4), 7, -8, 0x120, 0x403A, 2, 9),
                    (1, 2, 3, True, (3, 4), 7, -8, 0x120, 0x403A, "two", 9),
                    (1, 2, 3, 7, (-1, 9), 7, -8, None, None, None, 0),
                ),
            ),
            ((3, 4), ()),
            ((-1, 9), ((0, 0, 0, 0, (0, 0), 0, 0, 0, 0, 0, 0),)),
        ),
        ferries=(
            (node_row(base.source), (ferry_row(base), ferry_row(second))),
            (node_row(base.source), ()),
            (node_row(NavigationNode(0, 0, 0, 0, 0)), ()),
        ),
    )


def test_roundtrip_preserves_every_field_and_order() -> None:
    value = facts()
    decoded = codec.decode_transport_facts(codec.encode_transport_facts(value))
    assert type(decoded) is codec.StaticTransportFacts
    assert decoded == value
    assert type(decoded.elevators[0][1][0][3]) is int
    assert type(decoded.elevators[0][1][1][3]) is bool
    assert decoded.elevators[0][1][1][9] == "two"
    assert type(decoded.elevators[0][1][1][9]) is str
    assert decoded.ferries[0][1][0][1][1].origin == (4, 5, "script", 6, 7)
    assert decoded.ferries[0][1][0][6] == 2
    assert decoded.ferries[0][1][1][4] is None


def test_framing_and_nested_type_refusals() -> None:
    wire = codec.encode_transport_facts(facts())
    for mutate in (
        lambda w: w.update(extra=1),
        lambda w: w.update(version=True),
        lambda w: w.update(type="other"),
    ):
        bad = dict(wire)
        mutate(bad)
        with pytest.raises(CodecError):
            codec.decode_transport_facts(bad)

    bad = codec.encode_transport_facts(facts())
    bad["elevators"][0][1][0] = bad["elevators"][0][1][0][:10]
    with pytest.raises(CodecError):
        codec.decode_transport_facts(bad)

    bad = codec.encode_transport_facts(facts())
    bad["elevators"][0][1][0][1] = True
    with pytest.raises(CodecError):
        codec.decode_transport_facts(bad)

    bad = codec.encode_transport_facts(facts())
    bad["elevators"][0][1][1][3] = {"t": "none", "v": None}
    with pytest.raises(CodecError):
        codec.decode_transport_facts(bad)

    bad = codec.encode_transport_facts(facts())
    bad["ferries"][0][0] = [3, 4, 5]
    with pytest.raises(CodecError):
        codec.decode_transport_facts(bad)

    bad = codec.encode_transport_facts(facts())
    bad["ferries"][0][1][0][3] = True
    with pytest.raises(CodecError):
        codec.decode_transport_facts(bad)


def test_bounds_and_cycle_are_refused() -> None:
    wire = codec.encode_transport_facts(facts())
    wire["elevators"] = [[]] * (MAX_CONTAINER_LENGTH + 1)
    with pytest.raises(CodecError):
        codec.decode_transport_facts(wire)

    cyclic: list = []
    cyclic.append(cyclic)
    wire = codec.encode_transport_facts(facts())
    wire["ferries"] = cyclic
    with pytest.raises(CodecError):
        codec.decode_transport_facts(wire)


def test_encode_refuses_non_exact_owning_values() -> None:
    value = facts()
    with pytest.raises(CodecError):
        codec.encode_transport_facts(object())  # type: ignore[arg-type]
    with pytest.raises(CodecError):
        codec.encode_transport_facts(replace(value, elevators=list(value.elevators)))  # type: ignore[arg-type]
    with pytest.raises(CodecError):
        codec.encode_transport_facts(
            replace(value, ferries=((value.ferries[0][0], list(value.ferries[0][1])),))
        )  # type: ignore[arg-type]
    bad_route = replace(route(), selector=True)
    with pytest.raises(CodecError):
        codec.encode_transport_facts(
            codec.StaticTransportFacts(
                elevators=(), ferries=((node_row(route().source), (ferry_row(bad_route),)),)
            )
        )


def test_fresh_interpreter_imports_are_graph_free() -> None:
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "import pokegent.games.gen3.firered.rom.static_transport_codec\n"
        "assert not [m for m in sys.modules if m == 'pokegent.navigation' "
        "or m.startswith('pokegent.navigation.')]\n"
        "print('graph-free')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo)},
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "graph-free"
