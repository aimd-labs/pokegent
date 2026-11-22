"""Focused synthetic tests for the default-off materialization capture hooks."""

from __future__ import annotations

import inspect
import struct
from collections import Counter, defaultdict
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.rom.script_effects import interpret
from pokegent.navigation.graph import NavigationGraph, ScriptEffectStats

BASE = 0x08000000
ATTRIBUTES = {5: 0x1A2, 6: 0x0FF}


def setmetatile(x: int, y: int, metatile: int) -> bytes:
    return b"\xa2" + struct.pack("<HHHH", x, y, metatile, 0)


SCRIPT = setmetatile(1, 2, 5) + setmetatile(3, 4, 5) + setmetatile(5, 6, 6) + b"\x02"
UNKNOWN_SCRIPT = b"\xfe\x00\x00"


def state():
    return SimpleNamespace(
        identity=SimpleNamespace(group=1, number=2),
        grid=SimpleNamespace(attribute_map=dict(ATTRIBUTES)),
    )


def graph_for(*, materialize: bool, rom: bytes = SCRIPT) -> NavigationGraph:
    return NavigationGraph({}, rom=rom, provider=object(), _materialize=materialize)


def cache_keys(graph: NavigationGraph) -> list[tuple]:
    return list(graph._effect_cache)


def test_materialize_keyword_is_exact_bool_and_default_off() -> None:
    assert "_materialize" in inspect.signature(NavigationGraph.__init__).parameters
    default = graph_for(materialize=False)
    assert default._materialized is None
    assert default._materialized_audit is None

    collecting = graph_for(materialize=True)
    assert collecting._materialized == {}
    assert collecting._materialized_audit == []

    with pytest.raises(TypeError):
        NavigationGraph({}, provider=object(), _materialize=1)


def test_collectors_are_ready_before_the_first_constructor_reader(monkeypatch) -> None:
    seen: list[tuple[object, object]] = []
    original = NavigationGraph._read_seagallop_warps

    def observing(self):
        seen.append((self._materialized, self._materialized_audit))
        return original(self)

    monkeypatch.setattr(NavigationGraph, "_read_seagallop_warps", observing)
    graph = NavigationGraph({}, provider=object(), _materialize=True)
    assert seen == [({}, [])]
    assert graph._materialized == {}
    assert graph._materialized_audit == []


def test_cache_miss_records_real_reads_in_order_with_duplicates() -> None:
    graph = graph_for(materialize=True)
    st = state()
    result = graph._script_interpretation(BASE, st)

    assert len(graph._materialized) == 1
    key = cache_keys(graph)[0]
    stored_result, reads = graph._materialized[key]
    assert stored_result is result
    assert reads == ((5, 0x1A2), (5, 0x1A2), (6, 0x0FF))
    assert [effect.value("behavior") for effect in result.effects] == [0x1A2, 0x1A2, 0x0FF]


def test_cache_hit_adds_no_read_no_result_and_no_second_row() -> None:
    graph = graph_for(materialize=True)
    st = state()
    first = graph._script_interpretation(BASE, st)
    reads_after_miss = graph._materialized[cache_keys(graph)[0]][1]
    second = graph._script_interpretation(BASE, st)

    assert first is second
    assert len(graph._materialized) == 1
    assert graph._materialized[cache_keys(graph)[0]][1] == reads_after_miss
    assert len(graph._materialized[cache_keys(graph)[0]][1]) == 3


def test_default_and_capture_on_results_cache_readers_and_stats_agree() -> None:
    st = state()
    default = graph_for(materialize=False)
    collecting = graph_for(materialize=True)
    default_result = default._script_interpretation(BASE, st)
    on_result = collecting._script_interpretation(BASE, st)

    assert default_result == on_result
    assert cache_keys(default) == cache_keys(collecting)
    assert vars(default.script_trace_stats) == vars(collecting.script_trace_stats)
    assert dict(default.script_incomplete_counts) == dict(collecting.script_incomplete_counts)
    assert default._script_interpretation(BASE, st) is default_result
    assert collecting._script_interpretation(BASE, st) is on_result


def test_minimize_and_ferry_keys_keep_separate_rows() -> None:
    graph = graph_for(materialize=True)
    st = state()
    graph._script_interpretation(BASE, st)
    graph._script_interpretation(BASE, st, minimize_guards=False)
    graph._script_interpretation(BASE, st, ferry_mode=True)

    keys = set(graph._materialized)
    assert len(keys) == 3
    assert {(key[4], key[6]) for key in keys} == {(True, False), (False, False), (True, True)}


def test_complete_calls_record_no_audit_and_incomplete_calls_are_per_call() -> None:
    graph = graph_for(materialize=True)
    st = state()
    complete = graph._script_interpretation(BASE, st)
    assert complete.complete
    assert graph._complete_effects("elevators:bg", complete, state=st, site=(3, 4)) == (
        complete.effects
    )
    assert graph._materialized_audit == []

    real = interpret(UNKNOWN_SCRIPT, BASE)
    assert not real.complete
    first = replace(real, unknown=("reason A",))
    second = replace(real, unknown=("reason B",))
    assert graph._complete_effects("elevators:bg", first, state=st, site=(3, 4)) == ()
    assert graph._complete_effects("elevators:bg", second, state=st, site=(3, 4)) == ()
    assert graph._complete_effects("elevators:bg", first, state=st, site=(3, 4)) == ()

    assert [call.reasons for call in graph._materialized_audit] == [
        ("reason A",),
        ("reason B",),
        ("reason A",),
    ]
    assert graph.script_incomplete_counts["elevators:bg"] == 1
    assert graph._script_unknown_sites[((1, 2), (3, 4))] == {
        "script 0x8000000: reason A",
        "script 0x8000000: reason B",
    }


def test_two_instances_do_not_share_collectors() -> None:
    first = graph_for(materialize=True)
    second = graph_for(materialize=True)
    assert first._materialized is not second._materialized
    assert first._materialized_audit is not second._materialized_audit

    first._script_interpretation(BASE, state())
    first._complete_effects(
        "elevators:bg", interpret(UNKNOWN_SCRIPT, BASE), state=state(), site=(1, 1)
    )
    assert len(first._materialized) == 1
    assert len(first._materialized_audit) == 1
    assert second._materialized == {}
    assert second._materialized_audit == []


def test_old_uninitialized_fixture_remains_safe_without_the_new_fields() -> None:
    graph = NavigationGraph.__new__(NavigationGraph)
    graph.script_incomplete_counts = Counter({"elevators:bg": 0})
    graph._script_incomplete_seen = set()
    graph._script_unknown_sites = defaultdict(set)
    incomplete = interpret(UNKNOWN_SCRIPT, BASE)

    assert graph._complete_effects("elevators:bg", incomplete, state=state(), site=(1, 1)) == ()
    assert not hasattr(graph, "_materialized")
    assert not hasattr(graph, "_materialized_audit")

    graph._effect_cache = {}
    graph.rom = SCRIPT
    graph._profile = None
    graph._seagallop_warps = ()
    graph.script_trace_stats = ScriptEffectStats()
    result = graph._script_interpretation(BASE, state())
    assert result.complete
    assert not hasattr(graph, "_materialized")
