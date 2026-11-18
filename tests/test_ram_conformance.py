"""Directory selection and conformance failures use the actual offline library."""

import json
import struct

import pytest
from offline_ram import inventory, malformed_field_profile, run_tool, synthetic_inputs

from pokegent.games.gen3.firered.rom.conformance import MGBA_STATE_EWRAM_OFFSET
from tools import ram_conformance as tool
from tools import snapshot_dump


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    return synthetic_inputs(tmp_path, monkeypatch)


@pytest.mark.parametrize("limit,tested,code", [(0, 2, 1), (1, 1, 0), (2, 2, 1)])
def test_actual_selection_and_quiet(inputs, tmp_path, capsys, limit, tested, code):
    states, _, _, _ = inputs
    before = inventory(tmp_path)
    args = ["--states", str(states), "--profile", "synthetic", "--limit", str(limit)]
    assert tool.main(args) == code
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["tested"] == tested and report["failed"] == code
    assert [row["name"] for row in report["results"]] == ["a.state", "b.state"][:tested]
    assert str(tmp_path) not in output.out and not output.err
    if code:
        assert any("party_count" in failure for failure in report["results"][1]["failures"])
    assert tool.main([*args, "--quiet"]) == code
    quiet = json.loads(capsys.readouterr().out)
    assert quiet["tested"] == tested and quiet["failed"] == code
    assert quiet["results"] == [row for row in report["results"] if not row["ok"]]
    assert tool.main([*args, "--pattern", "a.*"]) == 0
    assert json.loads(capsys.readouterr().out)["tested"] == 1
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("kind", ["missing", "nonfile", "no-match", "empty", "truncated",
                                  "wrong-code", "unknown", "unsupported", "bad-profile",
                                  "bad-shape", "absolute-pattern", "bad-pattern", "file-dir"])
def test_errors_never_emit_partial_success(inputs, tmp_path, capsys, kind):
    states, good, bad, profile_path = inputs
    profile, extra = "synthetic", []
    if kind == "missing":
        states = tmp_path / "missing"
    elif kind == "nonfile":
        bad.unlink()
        bad.mkdir()
    elif kind == "no-match":
        extra = ["--pattern", "*.absent"]
    elif kind in ("empty", "truncated", "wrong-code"):
        raw = bytearray(good.read_bytes())
        raw[0x1C:0x20] = b"FAIL"
        bad.write_bytes(b"" if kind == "empty" else raw[:31] if kind == "truncated" else raw)
    elif kind == "bad-profile":
        profile_path.write_text("[]")
    elif kind == "bad-shape":
        profile_path.write_text("schema_version: 1\nprofiles:\n  synthetic: []\n")
    elif kind == "absolute-pattern":
        extra = ["--pattern", str(tmp_path / "*.state")]
    elif kind == "bad-pattern":
        extra = ["--pattern", "**bad"]
    elif kind == "file-dir":
        states = good
    else:
        profile = kind
    before = inventory(tmp_path)
    assert tool.main(["--states", str(states), "--profile", profile, *extra]) == 2
    output = capsys.readouterr()
    assert not output.out and output.err.startswith("error:")
    assert len(output.err) < 420 and "Traceback" not in output.err
    assert inventory(tmp_path) == before


def test_oserror_zero_tested_and_arguments(inputs, monkeypatch, capsys):
    args = ["--states", str(inputs[0]), "--profile", "synthetic"]
    def denied(*args, **kwargs):
        raise PermissionError("synthetic denied")
    monkeypatch.setattr(tool, "run_state_dir", denied)
    assert tool.main(args) == 2
    assert "synthetic denied" in capsys.readouterr().err
    monkeypatch.setattr(tool, "run_state_dir", lambda *a, **kw: {"tested": 0, "ok": True})
    assert tool.main(args) == 2
    assert "no states tested" in capsys.readouterr().err
    for argv in ([], [*args, "--limit", "-1"], [*args, "--limit", "bad"]):
        with pytest.raises(SystemExit) as exc:
            tool.main(argv)
        assert exc.value.code == 2


def test_subprocess_outside_checkout(inputs, tmp_path):
    args = ["--states", inputs[0], "--profile", "synthetic"]
    result = run_tool("ram_conformance.py", args, tmp_path)
    assert result.returncode == 1 and json.loads(result.stdout)["failed"] == 1
    result = run_tool("ram_conformance.py", [*args, "--limit", "1"], tmp_path)
    assert result.returncode == 0 and json.loads(result.stdout)["tested"] == 1
    result = run_tool("ram_conformance.py", [*args, "--pattern", "*.absent"], tmp_path)
    assert result.returncode == 2 and not result.stdout


def test_healthy_reader_can_fail_coordinate_conformance(inputs, capsys):
    states, good, _, _ = inputs
    raw = bytearray(good.read_bytes())
    raw[MGBA_STATE_EWRAM_OFFSET + 0x2000] = 1  # authored active player object
    raw[MGBA_STATE_EWRAM_OFFSET + 0x2002] = 1
    struct.pack_into("<hh", raw, MGBA_STATE_EWRAM_OFFSET + 0x2010, 8, 7)
    good.write_bytes(raw)
    assert snapshot_dump.main(["--ram-image", str(good), "--profile", "synthetic"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot"]["health"]["valid"]
    assert tool.main(["--states", str(states), "--profile", "synthetic", "--limit", "1"]) == 1
    row = json.loads(capsys.readouterr().out)["results"][0]
    assert row["failures"] == ["pos (0,0) != object event coords-7 (1,0)"]


def test_saved_party_mismatch_remains_informational(inputs, monkeypatch, capsys):
    report = dict(ok=True, tested=1, failed=0,
                  results=[dict(name="a.state", ok=True, failures=[],
                                summary=dict(saved_party_matches_live=False))])
    monkeypatch.setattr(tool, "run_state_dir", lambda *a, **kw: report)
    assert tool.main(["--states", str(inputs[0]), "--profile", "synthetic"]) == 0
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("subprocess_mode", [False, True])
@pytest.mark.parametrize("field", ["money", "location"])
def test_malformed_profile_field_width(inputs, tmp_path, capsys, subprocess_mode, field):
    malformed_field_profile(inputs[3], field)
    before = inventory(tmp_path)
    args = ["--states", str(inputs[0]), "--profile", "synthetic"]
    if subprocess_mode:
        result = run_tool("ram_conformance.py", args, tmp_path)
        code, out, err = result.returncode, result.stdout, result.stderr
    else:
        code = tool.main(args)
        output = capsys.readouterr()
        out, err = output.out, output.err
    assert code == 2 and out == ""
    message = "unpack_from" if field == "money" else "index out of range"
    assert err.startswith("error:") and message in err
    assert len(err) < 420 and "Traceback" not in err
    assert inventory(tmp_path) == before
