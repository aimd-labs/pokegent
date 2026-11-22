"""Process-cache behaviour for ``NavigationGraph.from_rom``.

Every test uses invented bytes, profiles and static reads; no ROM, local map
index or generated game data is involved.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.rom.rom_data import RomIdentity, RomProfile, SymbolEntry
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.navigation import graph as graph_module
from pokegent.navigation.graph import NavigationGraph


class _Unusable:
    usable = False
    state = None


class _CustomProvider:
    """A non-standard provider; the cache must never key or admit it."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def group_sizes(self) -> tuple[int, ...]:
        return (1,)

    def read_static(self, rom, profile, group, number):
        self.calls.append((group, number))
        return _Unusable()


class _BareProfile:
    """A profile object that cannot provide a canonical identity."""

    profile_id = "invented-bare"

    def has_symbol(self, name: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def isolated_cache():
    saved = dict(graph_module._ROM_GRAPH_CACHE)
    graph_module._ROM_GRAPH_CACHE.clear()
    yield
    graph_module._ROM_GRAPH_CACHE.clear()
    graph_module._ROM_GRAPH_CACHE.update(saved)


def _profile(profile_id: str = "invented", symbols: dict | None = None) -> RomProfile:
    return RomProfile(
        profile_id=profile_id,
        display_name=f"Invented {profile_id}",
        supported=False,
        rom=RomIdentity(sha1="0" * 40, sha256="0" * 64, game_code="TEST", version=0),
        symbols=symbols or {},
    )


class _MapIndex:
    """Invented loaded map index: group order and member names."""

    def __init__(self) -> None:
        self.sizes = [1]
        self.prefix = "INVENTED"

    def loaded(self) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        order = tuple(f"GROUP{index}" for index in range(len(self.sizes)))
        groups = tuple(
            tuple(f"{self.prefix}{index}_{number}" for number in range(size))
            for index, size in enumerate(self.sizes)
        )
        return order, groups


@pytest.fixture
def standard_reads(monkeypatch):
    """Stub the real standard provider's static reads, tables and loaded index."""
    calls: list[tuple[int, int]] = []
    index = _MapIndex()

    def read_static(rom, profile, group, number):
        calls.append((group, number))
        return _Unusable()

    monkeypatch.setattr(
        FireRedMapProvider, "group_sizes", staticmethod(lambda: tuple(index.sizes))
    )
    monkeypatch.setattr(FireRedMapProvider, "read_static", staticmethod(read_static))
    monkeypatch.setattr(map_names, "loaded_groups", index.loaded)
    monkeypatch.setattr(
        GameTables,
        "from_rom_image",
        staticmethod(lambda rom, profile: SimpleNamespace(items=[])),
    )
    return calls, index


def test_explicit_standard_provider_builds_without_cache_admission(monkeypatch, standard_reads):
    calls, _index = standard_reads
    profile = _profile()
    rom = b"\x01" * 16

    first = NavigationGraph.from_rom(rom, profile, provider=FireRedMapProvider())
    second = NavigationGraph.from_rom(rom, profile, provider=FireRedMapProvider())

    assert isinstance(first, NavigationGraph)
    assert first is not second
    assert len(calls) == 2
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_custom_provider_builds_without_cache_admission(standard_reads):
    profile = _profile()
    rom = b"\x02" * 16
    provider = _CustomProvider()

    first = NavigationGraph.from_rom(rom, profile, provider=provider)
    second = NavigationGraph.from_rom(rom, profile, provider=provider)

    assert first is not second
    assert first.provider is provider
    assert provider.calls == [(0, 0), (0, 0)]
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_graph_subclass_bypasses_cache(standard_reads):
    calls, _index = standard_reads
    profile = _profile()
    rom = b"\x0b" * 16

    class SubGraph(NavigationGraph):
        pass

    first = SubGraph.from_rom(rom, profile)
    second = SubGraph.from_rom(rom, profile)

    assert isinstance(first, SubGraph)
    assert first is not second
    assert len(calls) == 2
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_implicit_standard_provider_same_key_hits(standard_reads):
    calls, _index = standard_reads
    profile = _profile()
    rom = b"\x03" * 16

    first = NavigationGraph.from_rom(rom, profile)
    second = NavigationGraph.from_rom(rom, profile)

    assert first is second
    assert len(calls) == 1
    assert first.maps == {}


def test_changed_rom_misses(standard_reads):
    calls, _index = standard_reads
    profile = _profile()

    first = NavigationGraph.from_rom(b"\x04" * 16, profile)
    second = NavigationGraph.from_rom(b"\x05" * 16, profile)

    assert first is not second
    assert len(calls) == 2


def test_same_profile_id_with_changed_symbols_misses(standard_reads):
    calls, _index = standard_reads
    rom = b"\x06" * 16
    symbol = SymbolEntry(address=0x0800_0000, size=4, source="invented")
    first_profile = _profile()
    second_profile = _profile(symbols={"sInvented": symbol})

    first = NavigationGraph.from_rom(rom, first_profile)
    second = NavigationGraph.from_rom(rom, second_profile)

    assert first is not second
    assert len(calls) == 2


def test_loaded_grouping_change_misses(standard_reads):
    calls, index = standard_reads
    profile = _profile()
    rom = b"\x07" * 16

    first = NavigationGraph.from_rom(rom, profile)
    index.sizes[0] = 2
    second = NavigationGraph.from_rom(rom, profile)

    assert first is not second
    assert calls == [(0, 0), (0, 0), (0, 1)]


def test_same_group_counts_with_renamed_maps_misses(standard_reads):
    calls, index = standard_reads
    profile = _profile()
    rom = b"\x0c" * 16

    first = NavigationGraph.from_rom(rom, profile)
    index.prefix = "RENAMED"
    second = NavigationGraph.from_rom(rom, profile)

    assert first is not second
    assert len(calls) == 2


def test_source_fingerprint_change_misses(standard_reads, monkeypatch):
    calls, _index = standard_reads
    rom_cache = pytest.importorskip("pokegent.games.gen3.firered.rom.rom_cache")
    monkeypatch.setattr(rom_cache, "_generator_fingerprint", lambda: "first-source")
    profile = _profile()
    rom = b"\x08" * 16

    first = NavigationGraph.from_rom(rom, profile)
    monkeypatch.setattr(rom_cache, "_generator_fingerprint", lambda: "second-source")
    second = NavigationGraph.from_rom(rom, profile)

    assert first is not second
    assert len(calls) == 2


def test_fingerprint_failure_bypasses_cache(standard_reads, monkeypatch):
    calls, _index = standard_reads
    rom_cache = pytest.importorskip("pokegent.games.gen3.firered.rom.rom_cache")

    def failing_fingerprint():
        raise RuntimeError("invented source fingerprint failure")

    monkeypatch.setattr(rom_cache, "_generator_fingerprint", failing_fingerprint)
    profile = _profile()
    rom = b"\x0d" * 16

    first = NavigationGraph.from_rom(rom, profile)
    second = NavigationGraph.from_rom(rom, profile)

    assert first is not second
    assert len(calls) == 2
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_unavailable_identity_bypasses_instead_of_aliasing(standard_reads):
    calls, _index = standard_reads
    rom = b"\x09" * 16

    first = NavigationGraph.from_rom(rom, _BareProfile())
    second = NavigationGraph.from_rom(rom, _BareProfile())

    assert first is not second
    assert len(calls) == 2
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_loaded_grouping_inconsistent_with_provider_bypasses(standard_reads, monkeypatch):
    calls, index = standard_reads
    index.sizes[:] = [2]
    monkeypatch.setattr(
        map_names, "loaded_groups", lambda: (("GROUP0",), (("M0",),))
    )
    profile = _profile()
    rom = b"\x0e" * 16

    first = NavigationGraph.from_rom(rom, profile)
    second = NavigationGraph.from_rom(rom, profile)

    assert first is not second
    assert len(calls) == 4
    assert graph_module._ROM_GRAPH_CACHE == {}


def test_failed_construction_is_not_admitted(standard_reads, monkeypatch):
    calls, _index = standard_reads
    failures = [True]

    def read_static(rom, profile, group, number):
        calls.append((group, number))
        if failures[0]:
            failures[0] = False
            raise RuntimeError("invented static read failure")
        return _Unusable()

    monkeypatch.setattr(FireRedMapProvider, "read_static", staticmethod(read_static))
    profile = _profile()
    rom = b"\x0a" * 16

    with pytest.raises(RuntimeError):
        NavigationGraph.from_rom(rom, profile)
    assert graph_module._ROM_GRAPH_CACHE == {}

    built = NavigationGraph.from_rom(rom, profile)
    hit = NavigationGraph.from_rom(rom, profile)

    assert built is hit
    assert len(calls) == 2


def test_third_entry_evicts_oldest(standard_reads):
    calls, _index = standard_reads
    profile = _profile()
    roms = [bytes([index]) * 16 for index in (0x10, 0x11, 0x12)]

    built = [NavigationGraph.from_rom(rom, profile) for rom in roms]
    assert len(calls) == 3
    assert len(graph_module._ROM_GRAPH_CACHE) == 2

    NavigationGraph.from_rom(roms[0], profile)
    assert len(calls) == 4

    rebuilt_second = NavigationGraph.from_rom(roms[1], profile)
    assert len(calls) == 5

    assert rebuilt_second is NavigationGraph.from_rom(roms[1], profile)
    assert built[2] is not NavigationGraph.from_rom(roms[2], profile)
    assert len(calls) == 6


def test_digest_work_does_not_scale_with_map_count(standard_reads, monkeypatch):
    _calls, index = standard_reads
    profile = _profile()

    class CountingHashing:
        def __init__(self) -> None:
            self.calls = 0

        def sha256(self, data: bytes = b""):
            self.calls += 1
            return hashlib.sha256(data)

        def __getattr__(self, name: str):
            return getattr(hashlib, name)

    results = []
    for count in (1, 3):
        index.sizes[0] = count
        hashing = CountingHashing()
        monkeypatch.setattr(graph_module, "hashlib", hashing)
        NavigationGraph.from_rom(bytes([count]) * 16, profile)
        results.append(hashing.calls)

    assert results[0] == results[1]
