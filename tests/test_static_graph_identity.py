"""Focused synthetic tests for static-graph portable identity derivation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pokegent.games.gen3.firered.rom.static_graph_identity as identity
from pokegent.games.gen3.firered.maps import map_boulders, map_names
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.rom import rom_cache
from pokegent.games.gen3.firered.rom.static_graph_identity import (
    StaticIdentityError,
    derive_identity,
)

ORDER = ("Group_000", "Group_001")
GROUPS = (("Map_000", "Map_001"), ("Map_002",))

PUZZLE = b"""schema: boulders/1
maps:
  Puzzle_A:
    group: 0
    number: 0
    switches: []
    holes: []
    boulders: []
    closed_until_done: []
"""
OTHER_PUZZLE = PUZZLE.replace(b"Puzzle_A", b"Puzzle_B")
EMPTY_PUZZLE = b"schema: boulders/1\nmaps: {}\n"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeProfile:
    profile_id = "firered-us-rev0"

    def __init__(self, rom: bytes, marker: str = "profile", boom: bool = False) -> None:
        self.rom = SimpleNamespace(sha1=sha1(rom), sha256=sha256(rom))
        self.marker = marker
        self.boom = boom

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        assert mode == "json"
        if self.boom:
            raise MemoryError("synthetic profile allocation failure")
        return {
            "profile_id": self.profile_id,
            "marker": self.marker,
            "rom": {"sha1": self.rom.sha1, "sha256": self.rom.sha256},
        }


def index_document(order=ORDER, groups=GROUPS) -> dict[str, object]:
    return {
        "group_order": list(order),
        "groups": {name: list(members) for name, members in zip(order, groups, strict=True)},
        "label_kind": "numeric-id-v1",
    }


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def prepare(monkeypatch, tmp_path, *, rom, profile, index_doc=None, puzzle=PUZZLE,
            order=ORDER, groups=GROUPS, generator="d" * 64):
    monkeypatch.setattr(identity, "_generator_fingerprint", lambda: generator)

    def identify(data: bytes):
        return SimpleNamespace(
            supported=True,
            profile_id=profile.profile_id,
            sha1=sha1(data),
            sha256=sha256(data),
            rom=SimpleNamespace(sha1=sha1(data), sha256=sha256(data)),
        )

    monkeypatch.setattr(identity, "identify_rom_image", identify)
    monkeypatch.setattr(identity, "require_supported", lambda identification: profile)
    index_path = write(
        tmp_path / "index.json",
        json.dumps(index_document() if index_doc is None else index_doc).encode("utf-8"),
    )
    monkeypatch.setenv(map_names.MAP_GROUPS_PATH_ENV, str(index_path))
    if (order, groups) != (ORDER, GROUPS):
        monkeypatch.setattr(identity, "loaded_groups", lambda: (order, groups))
    puzzle_path = (
        None
        if puzzle is None
        else write(tmp_path / f"boulders-{sha256(puzzle)[:12]}.yaml", puzzle)
    )
    return index_path, puzzle_path


def derive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rom: bytes = b"synthetic-rom-image",
    profile: FakeProfile | None = None,
    provider=None,
    index_doc: dict[str, object] | None = None,
    puzzle: bytes | None = PUZZLE,
    order=ORDER,
    groups=GROUPS,
    generator: str = "d" * 64,
):
    profile = profile or FakeProfile(rom)
    index_path, puzzle_path = prepare(
        monkeypatch,
        tmp_path,
        rom=rom,
        profile=profile,
        index_doc=index_doc,
        puzzle=puzzle,
        order=order,
        groups=groups,
        generator=generator,
    )
    return derive_identity(
        rom,
        profile,
        FireRedMapProvider() if provider is None else provider,
        index_path=index_path,
        puzzle_path=puzzle_path,
    )


def test_portable_identity_is_exactly_the_eleven_v1_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    portable, digest = derive(monkeypatch, tmp_path)
    assert set(portable) == {
        "schema_version",
        "rom_sha1",
        "rom_sha256",
        "profile_sha256",
        "profile_id",
        "generator_sha256",
        "map_index_sha256",
        "group_order",
        "group_sizes",
        "grouping_sha256",
        "puzzle_sha256",
    }
    assert identity.static_artifact_loader._validate_identity(portable) == portable
    assert portable["schema_version"] == 1
    assert portable["rom_sha1"] == sha1(b"synthetic-rom-image")
    assert portable["rom_sha256"] == sha256(b"synthetic-rom-image")
    assert portable["group_order"] == list(ORDER)
    assert portable["group_sizes"] == [2, 1]
    assert portable["puzzle_sha256"] == sha256(PUZZLE)
    assert portable["generator_sha256"] == "d" * 64
    canonical = json.dumps(portable, sort_keys=True, separators=(",", ":"))
    assert digest == sha256(canonical.encode("utf-8"))


def test_rom_bytes_and_profile_each_drift_the_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, first_digest = derive(monkeypatch, tmp_path)
    second, second_digest = derive(monkeypatch, tmp_path, rom=b"other-rom-image")
    assert first["rom_sha1"] != second["rom_sha1"]
    assert first["rom_sha256"] != second["rom_sha256"]
    assert first_digest != second_digest

    rom = b"synthetic-rom-image"
    third, third_digest = derive(
        monkeypatch,
        tmp_path,
        rom=rom,
        profile=FakeProfile(rom, marker="other-profile"),
    )
    assert first["profile_sha256"] != third["profile_sha256"]
    assert first_digest != third_digest


def test_generator_and_index_bytes_drift_the_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, first_digest = derive(monkeypatch, tmp_path)
    second, second_digest = derive(monkeypatch, tmp_path, generator="e" * 64)
    assert first["generator_sha256"] != second["generator_sha256"]
    assert first_digest != second_digest

    spaced = json.dumps(index_document(), indent=2).encode("utf-8")
    third, third_digest = derive(monkeypatch, tmp_path, index_doc=json.loads(spaced))
    rom = b"synthetic-rom-image"
    profile = FakeProfile(rom)
    index_path, puzzle_path = prepare(
        monkeypatch, tmp_path, rom=rom, profile=profile, puzzle=PUZZLE
    )
    spaced_path = write(tmp_path / "spaced.json", spaced)
    monkeypatch.setenv(map_names.MAP_GROUPS_PATH_ENV, str(spaced_path))
    fourth, fourth_digest = derive_identity(
        rom,
        profile,
        FireRedMapProvider(),
        index_path=spaced_path,
        puzzle_path=puzzle_path,
    )
    assert index_path.name == "index.json"
    assert third["map_index_sha256"] != fourth["map_index_sha256"]
    assert third["grouping_sha256"] == fourth["grouping_sha256"]
    assert third_digest != fourth_digest


def test_puzzle_bytes_and_absent_vs_empty_are_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, first_digest = derive(monkeypatch, tmp_path)
    second, second_digest = derive(monkeypatch, tmp_path, puzzle=OTHER_PUZZLE)
    assert first["puzzle_sha256"] != second["puzzle_sha256"]
    assert first_digest != second_digest

    empty, _ = derive(monkeypatch, tmp_path, puzzle=EMPTY_PUZZLE)
    assert empty["puzzle_sha256"] == sha256(EMPTY_PUZZLE)
    assert empty["puzzle_sha256"] is not None


def test_absent_puzzle_is_null_without_default_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("default puzzle path or cache builder was called")

    monkeypatch.setattr(map_boulders, "default_puzzles_path", refuse)
    monkeypatch.setattr(rom_cache, "ensure", refuse)
    monkeypatch.setattr(map_boulders, "puzzles", refuse)
    portable, _ = derive(monkeypatch, tmp_path, puzzle=None)
    assert portable["puzzle_sha256"] is None


def test_full_same_size_group_rename_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    renamed_groups = (("Map_000", "Map_001"), ("Map_002_RENAMED",))
    with pytest.raises(StaticIdentityError, match="member names"):
        derive(monkeypatch, tmp_path, groups=renamed_groups)
    with pytest.raises(StaticIdentityError, match="order"):
        derive(monkeypatch, tmp_path, order=("Group_001", "Group_000"))


def test_provider_group_sizes_must_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(map_names, "group_sizes", lambda: (3, 0))
    with pytest.raises(StaticIdentityError, match="sizes"):
        derive(monkeypatch, tmp_path)


def test_actual_stale_lru_puzzle_table_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(identity, "_generator_fingerprint", lambda: "d" * 64)
    profile = FakeProfile(b"synthetic-rom-image")
    rom = b"synthetic-rom-image"

    def identify(data: bytes):
        return SimpleNamespace(
            supported=True, profile_id=profile.profile_id,
            sha1=sha1(data), sha256=sha256(data),
            rom=SimpleNamespace(sha1=sha1(data), sha256=sha256(data)),
        )

    monkeypatch.setattr(identity, "identify_rom_image", identify)
    monkeypatch.setattr(identity, "require_supported", lambda identification: profile)
    index_path = write(
        tmp_path / "index.json", json.dumps(index_document()).encode("utf-8")
    )
    monkeypatch.setenv(map_names.MAP_GROUPS_PATH_ENV, str(index_path))
    puzzle_path = write(tmp_path / "boulders.yaml", PUZZLE)
    map_boulders.puzzles(path=puzzle_path, allow_empty=True)
    write(puzzle_path, OTHER_PUZZLE)
    with pytest.raises(StaticIdentityError, match="differs"):
        derive_identity(
            rom,
            profile,
            FireRedMapProvider(),
            index_path=index_path,
            puzzle_path=puzzle_path,
        )


def test_effective_provider_table_must_be_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(map_boulders, "_load", lambda path: {})
    with pytest.raises(StaticIdentityError, match="differs"):
        derive(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"schema: other\nmaps: {}\n",
        b"not: [valid",
        b"schema: boulders/1\n",
        b"schema: boulders/1\nmaps: []\n",
    ],
)
def test_empty_or_malformed_puzzle_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
) -> None:
    with pytest.raises(StaticIdentityError, match="owner parsing"):
        derive(monkeypatch, tmp_path, puzzle=raw)


def test_unsupported_or_mismatched_rom_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(identity, "_generator_fingerprint", lambda: "d" * 64)
    index_path = write(tmp_path / "index.json", json.dumps(index_document()).encode())
    rom = b"synthetic-rom-image"
    profile = FakeProfile(rom)

    from pokegent.games.gen3.firered.rom.rom_data import (
        RomIdentification,
        RomStatus,
        UnsupportedRomError,
    )

    def unsupported(data: bytes):
        return RomIdentification(
            status=RomStatus.UNSUPPORTED_ROM,
            sha1=sha1(data),
            sha256=sha256(data),
            reasons=("synthetic",),
        )

    def reject(identification):
        raise UnsupportedRomError(identification)

    monkeypatch.setattr(identity, "identify_rom_image", unsupported)
    monkeypatch.setattr(identity, "require_supported", reject)
    with pytest.raises(StaticIdentityError, match="supported FireRed"):
        derive_identity(rom, profile, FireRedMapProvider(), index_path=index_path,
                        puzzle_path=None)

    other = FakeProfile(b"different-rom")
    monkeypatch.setattr(identity, "identify_rom_image", lambda data: SimpleNamespace(
        supported=True, profile_id="firered-us-rev0", sha1=sha1(data), sha256=sha256(data)
    ))
    monkeypatch.setattr(identity, "require_supported", lambda identification: other)
    with pytest.raises(StaticIdentityError, match="does not match"):
        derive_identity(rom, profile, FireRedMapProvider(), index_path=index_path,
                        puzzle_path=None)


def test_provider_subclass_and_missing_index_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class CustomProvider(FireRedMapProvider):
        @staticmethod
        def read_static(rom, profile, group, number):
            return object()

    monkeypatch.setattr(identity, "loaded_groups", lambda: (ORDER, GROUPS))
    with pytest.raises(StaticIdentityError, match="standard FireRed"):
        derive_identity(
            b"rom", FakeProfile(b"rom"), CustomProvider(),
            index_path=tmp_path / "x", puzzle_path=None,
        )
    with pytest.raises(StaticIdentityError, match="index path"):
        derive_identity(
            b"rom", FakeProfile(b"rom"), FireRedMapProvider(),
            index_path=None, puzzle_path=None,
        )


def test_memory_error_is_not_laundered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rom = b"synthetic-rom-image"
    with pytest.raises(MemoryError):
        derive(monkeypatch, tmp_path, rom=rom, profile=FakeProfile(rom, boom=True))

    def parser_boom(raw: bytes, *, source: str):
        raise MemoryError("synthetic parser allocation failure")

    monkeypatch.setattr(identity.map_boulders, "parse_puzzle_bytes", parser_boom)
    with pytest.raises(MemoryError):
        derive(monkeypatch, tmp_path, rom=rom)

    monkeypatch.undo()
    monkeypatch.setattr(map_boulders, "_load", lambda path: (_ for _ in ()).throw(
        MemoryError("synthetic provider allocation failure")
    ))
    with pytest.raises(MemoryError):
        derive(monkeypatch, tmp_path, rom=rom)

    monkeypatch.undo()
    monkeypatch.setattr(identity.static_artifact_loader, "_parse_json", lambda raw, label: (
        (_ for _ in ()).throw(MemoryError("synthetic index allocation failure"))
    ))
    with pytest.raises(MemoryError):
        derive(monkeypatch, tmp_path, rom=rom)


def test_typed_puzzle_projection_keeps_bool_int_and_tuple_list() -> None:
    assert identity._puzzle_digest({(0, 0): 1}) != identity._puzzle_digest({(0, 0): True})
    assert identity._puzzle_digest({(0, 0): (1,)}) != identity._puzzle_digest({(0, 0): [1]})


def test_typed_puzzle_projection_refuses_cycles_and_shared_dag_explosion() -> None:
    puzzle = map_boulders.PuzzleData("P", 0, 0, (), (), (), ())
    cycle: list[object] = []
    cycle.append(cycle)
    object.__setattr__(puzzle, "boulders", (cycle,))
    with pytest.raises(StaticIdentityError, match="cycle"):
        identity._puzzle_digest({(0, 0): puzzle})

    shared: list[object] = [0]
    for _ in range(6):
        shared = [shared] * 10
    with pytest.raises(StaticIdentityError, match="node budget"):
        identity._puzzle_digest({(0, 0): shared})
