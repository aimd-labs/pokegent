"""Focused synthetic tests for the unused per-call audit static-index codec."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.rom import static_audit_codec as codec
from pokegent.games.gen3.firered.rom.script_effects import Interpretation
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError
from pokegent.navigation import graph as navigation_graph


def calls() -> tuple[codec.AuditCall, ...]:
    return (
        codec.AuditCall("elevators:bg", (3, 4), (5, 6), 0x800100, ("reason A",)),
        codec.AuditCall("elevators:bg", (3, 4), (5, 6), 0x800100, ("reason B",)),
        codec.AuditCall("ferries:npc", (-1, 9), None, 0x800200, ()),
        codec.AuditCall("ferries:npc", (-1, 9), None, 0x800200, ()),
        codec.AuditCall("flag_items:bg", (0, 0), (0, 0), 0, ("x", "x", "y")),
    )


def test_reader_registry_matches_the_owning_source() -> None:
    assert codec.AUDIT_READERS == navigation_graph._SCRIPT_EFFECT_READERS
    assert len(codec.AUDIT_READERS) == 21


def test_roundtrip_preserves_per_call_groups_order_and_duplicates() -> None:
    value = calls()
    decoded = codec.decode_audit_calls(codec.encode_audit_calls(value))
    assert type(decoded) is tuple
    assert decoded == value
    assert [call.reasons for call in decoded[:2]] == [("reason A",), ("reason B",)]


def test_framing_reader_and_nested_type_refusals() -> None:
    wire = codec.encode_audit_calls(calls())
    for mutate in (
        lambda w: w.update(extra=1),
        lambda w: w.update(version=True),
        lambda w: w.update(type="other"),
    ):
        bad = dict(wire)
        mutate(bad)
        with pytest.raises(CodecError):
            codec.decode_audit_calls(bad)

    bad = codec.encode_audit_calls(calls())
    bad["calls"][0][0] = "unknown:reader"
    with pytest.raises(CodecError):
        codec.decode_audit_calls(bad)

    bad = codec.encode_audit_calls(calls())
    bad["calls"][0][4] = [1]
    with pytest.raises(CodecError):
        codec.decode_audit_calls(bad)

    bad = codec.encode_audit_calls(calls())
    bad["calls"][0][2] = [True, 6]
    with pytest.raises(CodecError):
        codec.decode_audit_calls(bad)

    with pytest.raises(CodecError):
        codec.encode_audit_calls((codec.AuditCall("unknown:reader", (3, 4), None, 1, ()),))


def test_actual_complete_effects_counterexample_keeps_two_reason_strings() -> None:
    graph = navigation_graph.NavigationGraph.__new__(navigation_graph.NavigationGraph)
    graph.script_incomplete_counts = Counter({name: 0 for name in codec.AUDIT_READERS})
    graph._script_incomplete_seen = set()
    graph._script_unknown_sites = defaultdict(set)
    state = SimpleNamespace(identity=SimpleNamespace(group=3, number=4))
    site = (5, 6)
    first = Interpretation(script_ptr=0x800100, effects=(), unknown=("reason A",))
    second = Interpretation(script_ptr=0x800100, effects=(), unknown=("reason B",))

    assert graph._complete_effects("elevators:bg", first, state=state, site=site) == ()
    assert graph._complete_effects("elevators:bg", second, state=state, site=site) == ()
    assert graph._complete_effects("elevators:bg", first, state=state, site=site) == ()

    assert graph.script_incomplete_counts["elevators:bg"] == 1
    assert graph._script_unknown_sites[((3, 4), site)] == {
        f"script {0x800100:#x}: reason A",
        f"script {0x800100:#x}: reason B",
    }
    persisted = (
        codec.AuditCall("elevators:bg", (3, 4), site, 0x800100, ("reason A",)),
        codec.AuditCall("elevators:bg", (3, 4), site, 0x800100, ("reason B",)),
    )
    decoded = codec.decode_audit_calls(codec.encode_audit_calls(persisted))
    assert decoded == persisted
    assert [call.reasons for call in decoded] == [("reason A",), ("reason B",)]
    assert "; ".join(decoded[0].reasons + decoded[1].reasons) == "reason A; reason B"


def test_fresh_interpreter_imports_are_graph_free() -> None:
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "import pokegent.games.gen3.firered.rom.static_audit_codec\n"
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
