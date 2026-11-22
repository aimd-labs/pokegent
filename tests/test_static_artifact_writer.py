"""Focused tests for the unused offline atomic static artifact writer."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.rom import static_artifact_loader as loader
from pokegent.games.gen3.firered.rom import static_artifact_writer as writer
from pokegent.games.gen3.firered.rom.script_effects import Interpretation
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


def canonical_sha(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def target(tmp_path: Path, ident: dict[str, object] | None = None) -> Path:
    return tmp_path / digest_for(identity() if ident is None else ident)


def build(directory: Path, payload: object, **overrides: object) -> Path:
    options: dict[str, object] = {
        "expected_identity": identity(),
        "counts": {"maps": 1, "interpretations": 1},
        "built_utc": "2026-11-22T00:00:00Z",
    }
    options.update(overrides)
    ident = options.pop("expected_identity")
    return writer.build_static_artifact(directory, ident, payload, **options)  # type: ignore[arg-type]


def load(directory: Path) -> dict:
    return loader.load_verified_document(directory, identity())


def test_round_trip_through_merged_loader_and_codec(tmp_path: Path) -> None:
    incomplete = Interpretation(
        script_ptr=0x100, effects=(), unknown=("unknown_cmd_0x99",), commands=1
    )
    payload = {
        "schema": "pokegent.static-graph",
        "maps": [],
        "nested": {"a": [1, True, None, "x"]},
        "interpretations": [encode_interpretation(incomplete)],
    }
    directory = target(tmp_path)
    receipt_path = build(directory, payload)

    assert receipt_path == directory / "receipt.json"
    assert load(directory) == payload
    decoded = loader.decode_interpretations(load(directory)["interpretations"])
    assert decoded[0].unknown == ("unknown_cmd_0x99",)
    assert decoded[0].effects == ()

    receipt = json.loads(receipt_path.read_text("utf-8"))
    assert set(receipt) == {
        "schema_version",
        "input_identity",
        "payload_name",
        "payload_sha256",
        "payload_size",
        "identity",
        "counts",
        "sealed",
        "built_utc",
    }
    assert receipt["sealed"] is True
    assert receipt["input_identity"] == digest_for(identity())
    assert receipt["payload_name"] == f"payload-{receipt['payload_sha256']}.json"
    assert receipt["payload_size"] == (directory / receipt["payload_name"]).stat().st_size
    assert stat_mode(directory) == 0o700
    assert stat_mode(directory / receipt["payload_name"]) == 0o600
    assert stat_mode(receipt_path) == 0o600
    assert list(directory.glob(".tmp-*")) == []


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_successive_payloads_preserve_old_payload(tmp_path: Path) -> None:
    directory = target(tmp_path)
    first = {"n": 1, "rows": [1, 2]}
    second = {"n": 2, "rows": [3, 4]}
    build(directory, first)
    old_name = f"payload-{canonical_sha(first)}.json"
    assert (directory / old_name).is_file()

    build(directory, second)
    assert (directory / old_name).is_file()
    assert load(directory) == second
    receipt = json.loads((directory / "receipt.json").read_text("utf-8"))
    assert receipt["payload_name"] == f"payload-{canonical_sha(second)}.json"


def test_same_payload_is_reused_after_verification(tmp_path: Path) -> None:
    directory = target(tmp_path)
    payload = {"same": "bytes"}
    build(directory, payload)
    name = f"payload-{canonical_sha(payload)}.json"
    before = os.stat(directory / name).st_ino
    build(directory, payload)
    assert os.stat(directory / name).st_ino == before
    assert load(directory) == payload


def test_creates_artifact_directory_mode_0700(tmp_path: Path) -> None:
    directory = target(tmp_path)
    build(directory, {"ok": True})
    assert stat_mode(directory) == 0o700


@pytest.mark.parametrize(
    ("target_name", "new_payload_visible"),
    [
        ("write", False),
        ("fsync", False),
        ("rename", False),
        ("replace", True),
    ],
)
def test_write_failure_keeps_old_receipt_and_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str, new_payload_visible: bool
) -> None:
    directory = target(tmp_path)
    old = {"generation": 1}
    new = {"generation": 2}
    build(directory, old)
    receipt_before = (directory / "receipt.json").read_bytes()
    old_name = f"payload-{canonical_sha(old)}.json"
    new_name = f"payload-{canonical_sha(new)}.json"
    assert old_name in os.listdir(directory)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(f"injected {target_name} failure")

    if target_name == "write":
        monkeypatch.setattr(writer.os, "write", boom)
    elif target_name == "fsync":
        monkeypatch.setattr(writer.os, "fsync", boom)
    elif target_name == "rename":
        monkeypatch.setattr(writer.os, "rename", boom)
    else:
        monkeypatch.setattr(writer.os, "replace", boom)

    with pytest.raises(writer.ArtifactWriteError):
        build(directory, new)

    assert (directory / "receipt.json").read_bytes() == receipt_before
    assert load(directory) == old
    assert (directory / old_name).is_file()
    assert (directory / new_name).is_file() is new_payload_visible
    assert list(directory.glob(".tmp-*")) == []


def test_post_receipt_fsync_failure_reports_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)
    payload = {"published": True}
    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("injected final directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", flaky)
    with pytest.raises(writer.ArtifactWriteError, match="uncertain"):
        build(directory, payload)
    assert load(directory) == payload


def test_payload_is_durable_before_receipt_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)
    payload = {"order": 1}
    events: list[tuple[str, str]] = []
    real_fsync, real_rename, real_replace = os.fsync, os.rename, os.replace

    def fsync(fd: int) -> None:
        events.append(("fsync", ""))
        real_fsync(fd)

    def rename(src: object, dst: object, *args: object, **kwargs: object) -> None:
        events.append(("rename", os.fspath(dst)))
        real_rename(src, dst, *args, **kwargs)

    def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        events.append(("replace", os.fspath(dst)))
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(writer.os, "fsync", fsync)
    monkeypatch.setattr(writer.os, "rename", rename)
    monkeypatch.setattr(writer.os, "replace", replace)
    build(directory, payload)

    payload_name = f"payload-{canonical_sha(payload)}.json"
    payload_index = events.index(("rename", payload_name))
    receipt_index = events.index(("replace", "receipt.json"))
    assert payload_index < receipt_index
    assert any(kind == "fsync" for kind, _ in events[:payload_index])
    assert any(kind == "fsync" for kind, _ in events[payload_index + 1 : receipt_index])


def test_existing_corrupt_payload_fails_closed(tmp_path: Path) -> None:
    directory = target(tmp_path)
    payload = {"n": 1}
    build(directory, payload)
    name = f"payload-{canonical_sha(payload)}.json"
    (directory / name).write_bytes(b"corrupt")
    receipt_before = (directory / "receipt.json").read_bytes()

    with pytest.raises(writer.ArtifactWriteError, match="differs"):
        build(directory, payload)
    assert (directory / name).read_bytes() == b"corrupt"
    assert (directory / "receipt.json").read_bytes() == receipt_before


def test_existing_payload_symlink_fails_closed(tmp_path: Path) -> None:
    directory = target(tmp_path)
    payload = {"n": 1}
    build(directory, payload)
    name = f"payload-{canonical_sha(payload)}.json"
    real = directory / name
    outside = tmp_path / "outside.json"
    outside.write_text("{}", "utf-8")
    real.unlink()
    real.symlink_to(outside)

    with pytest.raises(writer.ArtifactWriteError):
        build(directory, payload)
    assert real.is_symlink()
    assert outside.read_text("utf-8") == "{}"


def test_insecure_or_symlinked_directory_refuses(tmp_path: Path) -> None:
    directory = target(tmp_path)
    directory.mkdir()
    os.chmod(directory, 0o777)
    with pytest.raises(writer.ArtifactWriteError):
        build(directory, {"n": 1})

    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    link.symlink_to(real)
    with pytest.raises(writer.ArtifactWriteError):
        build(link, {"n": 1})


def test_unrelated_file_is_untouched(tmp_path: Path) -> None:
    directory = target(tmp_path)
    build(directory, {"n": 1})
    (directory / "notes.txt").write_text("keep", "utf-8")
    build(directory, {"n": 2})
    assert (directory / "notes.txt").read_text("utf-8") == "keep"


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"f": 1.5},
        {"nan": float("nan")},
        {"tuple": (1, 2)},
        {1: "non-str key"},
        {"deep": None},
    ],
)
def test_unsupported_payloads_refuse_before_publication(
    tmp_path: Path, bad_payload: dict
) -> None:
    if bad_payload == {"deep": None}:
        value: object = []
        for _ in range(loader.MAX_JSON_DEPTH + 1):
            value = [value]
        bad_payload = {"deep": value}
    directory = target(tmp_path)
    with pytest.raises(writer.ArtifactWriteError):
        build(directory, bad_payload)
    assert not directory.exists()


def test_cyclic_payload_refuses(tmp_path: Path) -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(writer.ArtifactWriteError, match="cycl"):
        build(target(tmp_path), cyclic)


def test_oversized_payload_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "PAYLOAD_MAX_BYTES", 64)
    with pytest.raises(writer.ArtifactWriteError):
        build(target(tmp_path), {"text": "x" * 100})


def test_number_token_bound_is_applied_before_publication(tmp_path: Path) -> None:
    directory = target(tmp_path)
    with pytest.raises(writer.ArtifactWriteError):
        build(directory, {"huge": int("9" * (loader.MAX_NUMBER_TOKEN + 1))})
    assert not directory.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"expected_identity": identity(rom_sha1="A" * 40)},
        {"counts": {"maps": -1}},
        {"counts": {"maps": True}},
        {"counts": {"k" * 65: 1}},
        {"built_utc": "2026-11-22 00:00:00"},
        {"built_utc": "2026-02-30T00:00:00Z"},
    ],
)
def test_invalid_caller_inputs_refuse(tmp_path: Path, override: dict) -> None:
    with pytest.raises(writer.ArtifactWriteError):
        build(target(tmp_path), {"n": 1}, **override)


def test_directory_basename_must_match_identity(tmp_path: Path) -> None:
    with pytest.raises(writer.ArtifactWriteError, match="basename"):
        build(tmp_path / "wrong-name", {"n": 1})


def test_reuse_fsyncs_payload_and_directory_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)
    payload = {"reuse": True}
    build(directory, payload)
    events: list[tuple[str, str]] = []
    real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename

    def fsync(fd: int) -> None:
        kind = "dir" if os.fstat(fd).st_mode & 0o040000 else "file"
        events.append(("fsync", kind))
        real_fsync(fd)

    def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        events.append(("replace", os.fspath(dst)))
        real_replace(src, dst, *args, **kwargs)

    def rename(src: object, dst: object, *args: object, **kwargs: object) -> None:
        events.append(("rename", os.fspath(dst)))
        real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(writer.os, "fsync", fsync)
    monkeypatch.setattr(writer.os, "replace", replace)
    monkeypatch.setattr(writer.os, "rename", rename)
    build(directory, payload)

    assert events[0] == ("fsync", "file")
    assert events[1] == ("fsync", "dir")
    assert not any(kind == "rename" for kind, _ in events)
    assert events.index(("replace", "receipt.json")) > 1


def test_reuse_fsync_failure_keeps_old_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)
    payload = {"reuse": "failure"}
    build(directory, payload)
    receipt_before = (directory / "receipt.json").read_bytes()
    name = f"payload-{canonical_sha(payload)}.json"
    payload_before = (directory / name).read_bytes()

    def boom(fd: int) -> None:
        raise OSError("injected reuse fsync failure")

    monkeypatch.setattr(writer.os, "fsync", boom)
    with pytest.raises(writer.ArtifactWriteError):
        build(directory, payload)

    assert (directory / "receipt.json").read_bytes() == receipt_before
    assert (directory / name).read_bytes() == payload_before
    assert load(directory) == payload
    assert list(directory.glob(".tmp-*")) == []


def test_new_directory_is_fsynced_in_parent_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)
    events: list[str] = []
    real_mkdir, real_fsync, real_rename, real_replace = (
        os.mkdir,
        os.fsync,
        os.rename,
        os.replace,
    )

    def mkdir(*args: object, **kwargs: object) -> None:
        events.append("mkdir")
        real_mkdir(*args, **kwargs)

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if os.fstat(fd).st_mode & 0o040000 else "fsync-file")
        real_fsync(fd)

    def rename(*args: object, **kwargs: object) -> None:
        events.append("rename")
        real_rename(*args, **kwargs)

    def replace(*args: object, **kwargs: object) -> None:
        events.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(writer.os, "mkdir", mkdir)
    monkeypatch.setattr(writer.os, "fsync", fsync)
    monkeypatch.setattr(writer.os, "rename", rename)
    monkeypatch.setattr(writer.os, "replace", replace)
    build(directory, {"new": True})

    assert events.index("mkdir") < events.index("fsync-dir") < events.index("rename")


def test_new_directory_parent_fsync_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = target(tmp_path)

    def boom(fd: int) -> None:
        raise OSError("injected parent fsync failure")

    monkeypatch.setattr(writer.os, "fsync", boom)
    with pytest.raises(writer.ArtifactWriteError, match="fsynced in its parent"):
        build(directory, {"new": True})
    assert not (directory / "receipt.json").exists()
    assert list(directory.glob("payload-*.json")) == []
    assert list(directory.glob(".tmp-*")) == []


def test_two_concurrent_publishers_leave_a_valid_artifact(tmp_path: Path) -> None:
    directory = target(tmp_path)
    build(directory, {"seed": True})
    first, second = {"writer": 1}, {"writer": 2}

    def publish(payload: dict) -> None:
        for _ in range(5):
            build(directory, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, first), pool.submit(publish, second)]
        for future in futures:
            future.result()

    assert load(directory) in (first, second)
    for payload in (first, second):
        assert (directory / f"payload-{canonical_sha(payload)}.json").is_file()
    assert list(directory.glob(".tmp-*")) == []
