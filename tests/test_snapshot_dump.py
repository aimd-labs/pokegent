"""Offline CLI contracts through the actual reader and authored temporary containers."""

import json

import pytest
from offline_ram import inventory, run_tool, synthetic_inputs

from tools import snapshot_dump as tool


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    return synthetic_inputs(tmp_path, monkeypatch)


def test_actual_snapshot_json_summary_and_read_only(inputs, tmp_path, capsys):
    _, good, bad, _ = inputs
    before = inventory(tmp_path)
    args = ["--ram-image", str(good), "--profile", "synthetic"]
    assert tool.main(args) == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert not output.err
    assert data["meta"] == {"source": "ram-image", "file": "a.state"}
    assert str(tmp_path) not in output.out
    assert data["snapshot"]["health"]["valid"]
    assert data["snapshot"]["party"] == []
    assert tool.main([*args, "--summary", "--party-source", "saveblock"]) == 0
    assert "party: empty" in capsys.readouterr().out
    assert tool.main(["--ram-image", str(bad), "--profile", "synthetic"]) == 1
    snapshot = json.loads(capsys.readouterr().out)["snapshot"]
    assert not snapshot["health"]["valid"]
    assert snapshot["health"]["read_errors"][0]["code"] == "party_count_invalid"
    assert snapshot["player"] is None and snapshot["party"] == []
    assert tool.main(["--ram-image", str(bad), "--profile", "synthetic", "--summary"]) == 1
    summary = capsys.readouterr().out
    assert "INVALID" in summary and "party_count" in summary and "player:" not in summary
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "truncated", "wrong-code",
                                  "unknown-profile", "unsupported", "bad-profile", "bad-shape"])
def test_input_errors(inputs, tmp_path, capsys, monkeypatch, kind):
    _, good, _, profile_path = inputs
    path, profile = good, "synthetic"
    if kind == "missing":
        path = tmp_path / "absent.state"
    elif kind == "directory":
        path = tmp_path
    elif kind in ("empty", "truncated", "wrong-code"):
        data = bytearray(good.read_bytes())
        data[0x1C:0x20] = b"FAIL"
        path = tmp_path / "invalid.state"
        path.write_bytes(b"" if kind == "empty" else data[:31] if kind == "truncated" else data)
    elif kind == "bad-profile":
        profile_path.write_text("profiles: [")
    elif kind == "bad-shape":
        profile_path.write_text("schema_version: 1\nprofiles:\n  synthetic: []\n")
    else:
        profile = "unknown" if kind == "unknown-profile" else "unsupported"
    before = inventory(tmp_path)
    assert tool.main(["--ram-image", str(path), "--profile", profile]) == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err.startswith("error:")
    assert len(output.err) < 420 and "Traceback" not in output.err
    assert inventory(tmp_path) == before


def test_oserror_and_cli_arguments(inputs, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise PermissionError("synthetic denied")
    monkeypatch.setattr(tool, "load_mgba_state_ram", denied)
    assert tool.main(["--ram-image", str(inputs[1]), "--profile", "synthetic"]) == 2
    assert "synthetic denied" in capsys.readouterr().err
    for args in ([], ["--ram-image", "x", "--party-source", "bad"], ["--state", "x"]):
        with pytest.raises(SystemExit) as exc:
            tool.main(args)
        assert exc.value.code == 2


@pytest.mark.parametrize("bad", [False, True])
def test_subprocess_outside_checkout(inputs, tmp_path, bad):
    result = run_tool("snapshot_dump.py", ["--ram-image", inputs[2 if bad else 1],
                      "--profile", "synthetic"], tmp_path)
    assert result.returncode == int(bad)
    assert json.loads(result.stdout)["snapshot"]["health"]["valid"] is not bad
    result = run_tool("snapshot_dump.py", ["--ram-image", tmp_path / "missing.state"], tmp_path)
    assert result.returncode == 2 and not result.stdout
