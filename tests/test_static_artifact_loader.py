"""Focused tests for the unused bounded static artifact loader."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.rom.script_effects import (
    Effect,
    Guard,
    Interpretation,
)
from pokegent.games.gen3.firered.rom.static_artifact_loader import (
    PAYLOAD_MAX_BYTES,
    RECEIPT_MAX_BYTES,
    ArtifactLoadError,
    decode_interpretations,
    load_verified_document,
)
from pokegent.games.gen3.firered.rom.static_symbolic_codec import encode_interpretation

HEX40 = "a" * 40


def identity(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "rom_sha1": HEX40,
        "rom_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
        "profile_id": "firered-us-rev0",
        "generator_sha256": "d" * 64,
        "map_index_sha256": "e" * 64,
        "group_order": ["Group_000", "Group_001"],
        "group_sizes": [2, 1],
        "grouping_sha256": "f" * 64,
        "puzzle_sha256": None,
    }
    value.update(overrides)
    return value


def digest_for(value: dict[str, object]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def receipt_for(
    ident: dict[str, object], payload: bytes, *, overrides: dict[str, object] | None = None
) -> dict[str, object]:
    payload_sha = hashlib.sha256(payload).hexdigest()
    value: dict[str, object] = {
        "schema_version": 1,
        "input_identity": digest_for(ident),
        "payload_name": f"payload-{payload_sha}.json",
        "payload_sha256": payload_sha,
        "payload_size": len(payload),
        "identity": ident,
        "counts": {"maps": 1},
        "sealed": True,
        "built_utc": "2026-11-22T00:00:00Z",
    }
    if overrides:
        value.update(overrides)
    return value


def artifact(
    tmp_path: Path,
    *,
    ident: dict[str, object] | None = None,
    payload_obj: object | None = None,
    payload_bytes: bytes | None = None,
    receipt_overrides: dict[str, object] | None = None,
    directory_name: str | None = None,
) -> tuple[Path, dict[str, object]]:
    ident = identity() if ident is None else ident
    if payload_bytes is None:
        payload_bytes = json.dumps(
            {} if payload_obj is None else payload_obj, separators=(",", ":")
        ).encode("utf-8")
    name = digest_for(ident) if directory_name is None else directory_name
    directory = tmp_path / name
    directory.mkdir(parents=True)
    os.chmod(directory, 0o700)
    receipt = receipt_for(ident, payload_bytes, overrides=receipt_overrides)
    (directory / receipt["payload_name"]).write_bytes(payload_bytes)
    (directory / "receipt.json").write_bytes(
        json.dumps(receipt, separators=(",", ":")).encode("utf-8")
    )
    os.chmod(directory / receipt["payload_name"], 0o644)
    os.chmod(directory / "receipt.json", 0o644)
    return directory, receipt


def load(directory: Path, ident: dict[str, object] | None = None) -> dict:
    return load_verified_document(directory, identity() if ident is None else ident)


def test_valid_document_loads_verified_payload(tmp_path: Path) -> None:
    payload = {"schema": "pokegent.static-graph", "maps": []}
    directory, _ = artifact(tmp_path, payload_obj=payload)
    assert load(directory) == payload


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", 2),
        ("rom_sha1", "0" * 40),
        ("rom_sha256", "0" * 64),
        ("profile_sha256", "0" * 64),
        ("profile_id", "other-profile"),
        ("generator_sha256", "0" * 64),
        ("map_index_sha256", "0" * 64),
        ("group_order", ["Group_000"]),
        ("group_sizes", [2, 2]),
        ("grouping_sha256", "0" * 64),
        ("puzzle_sha256", "0" * 64),
    ],
)
def test_supplied_identity_must_match_receipt_identity(
    tmp_path: Path, field: str, replacement: object
) -> None:
    directory, _ = artifact(tmp_path)
    with pytest.raises(ArtifactLoadError):
        load(directory, identity(**{field: replacement}))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("group_sizes"),
        lambda value: value.update(extra=1),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(rom_sha1="A" * 40),
        lambda value: value.update(group_sizes=[2]),
        lambda value: value.update(group_sizes=[True, 1]),
        lambda value: value.update(group_order=["Group_000", "Group_000"]),
        lambda value: value.update(grouping_sha256="f" * 63),
        lambda value: value.update(puzzle_sha256=""),
        lambda value: (value.pop("group_order"), value.update({1: ["Group_000"]})),
    ],
)
def test_malformed_expected_identity_refuses(tmp_path: Path, mutation) -> None:
    directory, _ = artifact(tmp_path)
    bad = identity()
    mutation(bad)
    with pytest.raises(ArtifactLoadError):
        load(directory, bad)


def test_directory_basename_must_equal_identity_digest(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path, directory_name="not-the-digest")
    with pytest.raises(ArtifactLoadError):
        load(directory)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sealed": False},
        {"sealed": 1},
        {"schema_version": 2},
        {"schema_version": True},
        {"payload_size": True},
        {"payload_size": -1},
        {"payload_sha256": "0" * 64},
        {"payload_name": f"payload-{'0' * 64}.json"},
        {"input_identity": "0" * 64},
        {"counts": {"maps": 1, "x": True}},
        {"counts": {"maps": -1}},
        {"counts": "maps"},
        {"built_utc": "2026-11-22 00:00:00"},
        {"extra": "field"},
        {"payload_size": PAYLOAD_MAX_BYTES + 1},
    ],
)
def test_malformed_receipt_refuses(tmp_path: Path, overrides: dict[str, object]) -> None:
    directory, _ = artifact(tmp_path, receipt_overrides=overrides)
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_missing_required_receipt_key_refuses(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    data = json.loads((directory / "receipt.json").read_text())
    del data["counts"]
    (directory / "receipt.json").write_text(json.dumps(data))
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_duplicate_receipt_key_refuses(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    (directory / "receipt.json").write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ArtifactLoadError):
        load(directory)


@pytest.mark.parametrize(
    "payload_bytes",
    [
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":0.5}',
        b'{"x":1,"x":2}',
        b'{"x":"\xff"}',
        b'{"x":' + b"9" * 129 + b"}",
    ],
)
def test_malformed_payload_refuses(tmp_path: Path, payload_bytes: bytes) -> None:
    directory, _ = artifact(tmp_path, payload_bytes=payload_bytes)
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_payload_bytes_are_hashed_before_parse(tmp_path: Path, monkeypatch) -> None:
    directory, receipt = artifact(tmp_path, payload_obj={"maps": []})
    import pokegent.games.gen3.firered.rom.static_artifact_loader as module

    calls = 0
    real_loads = module.json.loads

    def spy(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(module.json, "loads", spy)
    payload = directory / receipt["payload_name"]
    original = payload.read_bytes()
    payload.write_bytes(original.replace(b"maps", b"Maps"))
    with pytest.raises(ArtifactLoadError):
        load(directory)
    assert calls == 1  # receipt only; payload digest mismatch stopped parsing


def test_json_nesting_bound_before_parse(tmp_path: Path, monkeypatch) -> None:
    def nested(depth: int) -> bytes:
        return b'{"d":' + b"[" * depth + b"0" + b"]" * depth + b"}"

    ok_dir, _ = artifact(tmp_path / "ok", payload_bytes=nested(127))
    assert load(ok_dir)["d"]

    deep_dir, _ = artifact(tmp_path / "deep", payload_bytes=nested(128))
    import pokegent.games.gen3.firered.rom.static_artifact_loader as module

    calls = 0
    real_loads = module.json.loads

    def spy(text, *args, **kwargs):
        nonlocal calls
        calls += 1
        if '"d"' in text:
            raise AssertionError("json.loads called before nesting bound")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(module.json, "loads", spy)
    with pytest.raises(ArtifactLoadError, match="nesting"):
        load(deep_dir)
    assert calls == 1  # receipt only; payload was refused pre-parse


def test_brackets_and_escapes_in_strings_do_not_affect_depth(tmp_path: Path) -> None:
    payload = b'{"s":"[[[\\\\\\"]\\\\]]","d":[[0]]}'
    directory, _ = artifact(tmp_path, payload_bytes=payload)
    assert load(directory) == {"s": '[[[\\"]\\]]', "d": [[0]]}


def test_token_limit_rejects_before_parse(tmp_path: Path, monkeypatch) -> None:
    directory, _ = artifact(tmp_path, payload_obj={"a": 1})
    import pokegent.games.gen3.firered.rom.static_artifact_loader as module

    monkeypatch.setattr(module, "MAX_JSON_TOKENS", 1_000)
    calls = 0
    real_loads = module.json.loads

    def spy(text, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_loads(text, *args, **kwargs)
        monkeypatch.setattr(module, "MAX_JSON_TOKENS", 3)
        return result

    monkeypatch.setattr(module.json, "loads", spy)
    with pytest.raises(ArtifactLoadError, match="token"):
        load(directory)
    assert calls == 1  # receipt only; payload was refused pre-parse


def test_receipt_byte_bound(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    receipt = directory / "receipt.json"
    receipt.write_bytes(b" " * (RECEIPT_MAX_BYTES + 1))
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_payload_byte_bound(tmp_path: Path, monkeypatch) -> None:
    directory, receipt = artifact(tmp_path, payload_obj={"a": 1})
    import pokegent.games.gen3.firered.rom.static_artifact_loader as module

    monkeypatch.setattr(module, "PAYLOAD_MAX_BYTES", 4)
    with pytest.raises(ArtifactLoadError):
        load(directory)


@pytest.mark.parametrize("target", ["receipt", "payload"])
def test_symlink_refuses(tmp_path: Path, target: str) -> None:
    directory, receipt = artifact(tmp_path)
    real = tmp_path / f"real-{target}"
    name = "receipt.json" if target == "receipt" else receipt["payload_name"]
    path = directory / name
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(ArtifactLoadError):
        load(directory)


@pytest.mark.parametrize("target", ["receipt", "payload"])
def test_nonregular_file_refuses(tmp_path: Path, target: str) -> None:
    directory, receipt = artifact(tmp_path)
    name = "receipt.json" if target == "receipt" else receipt["payload_name"]
    path = directory / name
    path.unlink()
    path.mkdir()
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_fifo_receipt_refuses_without_blocking(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    receipt = directory / "receipt.json"
    receipt.unlink()
    os.mkfifo(receipt, 0o644)
    repo = Path(__file__).resolve().parents[1]
    code = """
import json, sys
from pathlib import Path
from pokegent.games.gen3.firered.rom.static_artifact_loader import (
    ArtifactLoadError,
    load_verified_document,
)
try:
    load_verified_document(Path(sys.argv[1]), json.loads(sys.argv[2]))
except ArtifactLoadError:
    print("refused")
else:
    raise SystemExit("admitted a fifo receipt")
"""
    env = {**os.environ, "PYTHONPATH": str(repo)}
    result = subprocess.run(
        [sys.executable, "-c", code, str(directory), json.dumps(identity())],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"


def test_group_or_world_writable_file_refuses(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    os.chmod(directory / "receipt.json", 0o666)
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_symlink_artifact_directory_refuses(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path / "real")
    link = tmp_path / directory.name
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ArtifactLoadError, match="opened safely"):
        load(link)


def test_group_or_world_writable_directory_refuses(tmp_path: Path) -> None:
    directory, _ = artifact(tmp_path)
    os.chmod(directory, 0o770)
    with pytest.raises(ArtifactLoadError, match="group/world writable"):
        load(directory)


def test_directory_not_owned_by_current_user_refuses(tmp_path: Path, monkeypatch) -> None:
    directory, _ = artifact(tmp_path)
    import pokegent.games.gen3.firered.rom.static_artifact_loader as module

    real_uid = os.getuid()
    monkeypatch.setattr(module.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ArtifactLoadError, match="not owned"):
        load(directory)


def test_missing_payload_refuses_before_scan(tmp_path: Path) -> None:
    directory, receipt = artifact(tmp_path)
    (directory / receipt["payload_name"]).unlink()
    (directory / "payload-other.json").write_bytes(b"{}")
    with pytest.raises(ArtifactLoadError):
        load(directory)


def test_inputs_and_mtimes_unchanged(tmp_path: Path) -> None:
    directory, receipt = artifact(tmp_path, payload_obj={"a": 1})
    paths = [directory / "receipt.json", directory / receipt["payload_name"]]
    before = [(p.stat().st_mtime_ns, p.stat().st_size) for p in paths]
    load(directory)
    after = [(p.stat().st_mtime_ns, p.stat().st_size) for p in paths]
    assert before == after


def interpretation(*guards: Guard, decoded: bool = True) -> Interpretation:
    effect = Effect(
        kind="test",
        source=1,
        guards=tuple(guards),
        values=(),
        decoded=decoded,
        reason=None if decoded else "partial",
    )
    return Interpretation(script_ptr=1, effects=(effect,), unknown=(), commands=1)


@pytest.mark.parametrize("operator", ["lt", "eq", "gt", "le", "ge", "ne"])
def test_all_current_operators_admitted(operator: str) -> None:
    encoded = encode_interpretation(interpretation(Guard("flag", 1, operator, True)))
    assert len(decode_interpretations([encoded])) == 1


@pytest.mark.parametrize("position", ["second_guard", "second_effect"])
def test_unknown_operator_refuses_everywhere(position: str) -> None:
    good = Guard("flag", 1, "eq", True)
    bad = Guard("flag", 2, "zz", True)
    if position == "second_guard":
        value = interpretation(good, bad)
    else:
        second = Effect(
            kind="test",
            source=2,
            guards=(bad,),
            values=(),
            decoded=True,
            reason=None,
        )
        first = interpretation(good)
        value = Interpretation(
            script_ptr=1,
            effects=(first.effects[0], second),
            unknown=(),
            commands=2,
        )
    with pytest.raises(ArtifactLoadError):
        decode_interpretations([encode_interpretation(value)])


def test_incomplete_interpretation_retention() -> None:
    value = Interpretation(
        script_ptr=7,
        effects=(
            Effect(
                kind="test",
                source=1,
                guards=(Guard("flag", 1, "eq", True),),
                values=(),
                decoded=False,
                reason="unknown branch",
            ),
        ),
        unknown=("unsupported command",),
        commands=3,
    )
    (decoded,) = decode_interpretations([encode_interpretation(value)])
    assert decoded == value
    assert not decoded.complete
    assert decoded.effects[0].reason == "unknown branch"


def test_decode_interpretations_refuses_malformed_rows() -> None:
    with pytest.raises(ArtifactLoadError):
        decode_interpretations({"not": "a list"})
    with pytest.raises(ArtifactLoadError):
        decode_interpretations([{"version": 1}])
