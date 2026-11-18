"""Actual library conformance and raw-loader boundaries with invented inputs."""

import pytest

from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom.conformance import (
    MGBA_STATE_SIZE,
    StateFormatError,
    check_port,
    load_mgba_state_ram,
    run_conformance,
)


@pytest.mark.parametrize("case", ["identical", "different", "saved_invalid", "live_invalid"])
def test_check_port_uses_actual_live_and_saved_readers(ram, encode_mon, case):
    profile, port, sb1, _ = ram
    port.poke(profile.symbol("gPlayerPartyCount").address, b"\x01")
    port.poke(profile.symbol("gPlayerParty").address, encode_mon(species=17))
    saved_count = sb1 + profile.field("SaveBlock1", "playerPartyCount").offset
    saved_party = sb1 + profile.field("SaveBlock1", "playerParty").offset
    port.poke(saved_count, b"\x01")
    port.poke(saved_party, encode_mon(species=17))
    baseline = check_port("baseline", port, profile)
    assert baseline["ok"] and baseline["summary"]["saved_party_matches_live"] is True
    if case == "different":
        port.poke(saved_party, encode_mon(species=29))
    elif case == "saved_invalid":
        port.poke(saved_count, b"\x07")
    elif case == "live_invalid":
        port.poke(profile.symbol("gPlayerPartyCount").address, b"\x07")
    result = check_port(case, port, profile)
    assert result["name"] == case
    assert result["ok"] is (case in ("identical", "different"))
    if case == "live_invalid":
        assert "summary" not in result
    else:
        assert result["summary"]["party"] == [(17, 37, 71, 113)]
        assert result["summary"]["saved_party_matches_live"] is (case == "identical")
    if case in ("live_invalid", "saved_invalid"):
        label = "live" if case == "live_invalid" else "saveblock"
        assert len(result["failures"]) == 1
        assert result["failures"][0].startswith(f"{label}: party/party_count_invalid:")
    else:
        assert result["failures"] == []


def test_run_conformance_counts_and_preserves_named_port_order(ram):
    profile, port, sb1, _ = ram
    good_regions = {0x02000000: port.read_range(0x02000000, 0x40000),
                    0x03000000: port.read_range(0x03000000, 0x8000)}
    saved_bad = SnapshotMemoryReader(good_regions)
    saved_bad.poke(sb1 + profile.field("SaveBlock1", "playerPartyCount").offset, b"\x07")
    live_bad = SnapshotMemoryReader(good_regions)
    live_bad.poke(profile.symbol("gPlayerPartyCount").address, b"\x07")
    report = run_conformance(iter((("good", port), ("saved-bad", saved_bad),
                                   ("live-bad", live_bad))), profile)
    assert (report["ok"], report["tested"], report["failed"]) == (False, 3, 2)
    assert [(r["name"], r["ok"]) for r in report["results"]] == [
        ("good", True), ("saved-bad", False), ("live-bad", False)
    ]
    good = run_conformance([("good", port)], profile)
    assert (good["ok"], good["tested"], good["failed"]) == (True, 1, 0)


@pytest.mark.parametrize("game_code", [b"BPRE", b"UNIT"])
def test_raw_loader_maps_section_sentinels_and_leaves_input_unchanged(tmp_path, game_code):
    raw = bytearray(MGBA_STATE_SIZE)
    raw[0x1C:0x20] = game_code
    # Explicit current container format; no binary fixture or generated game data.
    for offset, marker in ((0x19000, 0x31), (0x20FFF, 0x42),
                           (0x21000, 0x53), (0x60FFF, 0x64)):
        raw[offset] = marker
    path = tmp_path / "authored.state"
    path.write_bytes(raw)
    path.chmod(0o444)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    port = load_mgba_state_ram(path, game_code=game_code)
    assert [port.read_u8(a) for a in (0x03000000, 0x03007FFF, 0x02000000, 0x0203FFFF)] == [
        0x31, 0x42, 0x53, 0x64
    ]
    port.poke(0x02000000, b"\x99")  # The returned RAM is detached from the input file.
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before


@pytest.mark.parametrize("delta,code", [(-1, b"BPRE"), (1, b"BPRE"), (0, b"FAIL")])
def test_raw_loader_rejects_wrong_size_or_code_without_writes(tmp_path, delta, code):
    raw = bytearray(MGBA_STATE_SIZE + delta)
    raw[0x1C:0x20] = code
    path = tmp_path / "invalid.state"
    path.write_bytes(raw)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(StateFormatError, match="size" if delta else "game code") as caught:
        load_mgba_state_ram(path)
    assert type(caught.value) is StateFormatError
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_raw_loader_preserves_missing_path_error(tmp_path):
    path = tmp_path / "missing.state"
    with pytest.raises(FileNotFoundError) as caught:
        load_mgba_state_ram(path)
    assert caught.value.filename == str(path) and not path.exists()
