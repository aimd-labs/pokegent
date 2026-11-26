"""Synthetic wheel manifest checks; no build, extraction, network or game input."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import warnings
import zipfile
import zlib
from pathlib import Path

import pytest

from tools import wheel_manifest as manifest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "wheel_manifest.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIST = "synthetic_pkg-0.1.0.dist-info"
METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: synthetic-pkg\n"
    "Version: 0.1.0\n\n"
)
WHEEL_TEXT = (
    "Wheel-Version: 1.0\n"
    "Generator: synthetic-builder (1.0)\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n\n"
)
DEFAULT_MEMBERS = {
    "synthetic_pkg/__init__.py": b"VALUE = 1\n",
    "synthetic_pkg/core.py": b"def value():\n    return 2\n",
}


def write_wheel(path, members=None, metadata=METADATA, wheel_file=WHEEL_TEXT,
                extra_metadata=None, compression=zipfile.ZIP_DEFLATED):
    members = DEFAULT_MEMBERS if members is None else members
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if metadata is not None:
            archive.writestr(f"{DIST}/METADATA", metadata)
        if wheel_file is not None:
            archive.writestr(f"{DIST}/WHEEL", wheel_file)
        if extra_metadata is not None:
            archive.writestr("other_pkg-1.0.dist-info/METADATA", extra_metadata)
    return Path(path)


def by_name(report):
    return {item["name"]: item for item in report["files"]}


def test_manifest_is_deterministic_sorted_and_labelled(tmp_path):
    wheel = write_wheel(tmp_path / "synthetic-0.1.0.whl")
    first = manifest.build_manifest(wheel, source_commit=COMMIT)
    second = manifest.build_manifest(wheel, source_commit=COMMIT)
    copied = tmp_path / "copy.whl"
    copied.write_bytes(wheel.read_bytes())
    third = manifest.build_manifest(copied, source_commit=COMMIT)

    encoded = json.dumps(first, sort_keys=True)
    assert encoded == json.dumps(second, sort_keys=True)
    assert encoded == json.dumps(third, sort_keys=True)
    assert "timestamp" not in encoded and "date_time" not in encoded
    assert str(wheel) not in encoded

    names = [item["name"] for item in first["files"]]
    assert names == sorted(names)
    assert set(names) == set(DEFAULT_MEMBERS) | {
        f"{DIST}/METADATA", f"{DIST}/WHEEL",
    }
    assert first["wheel"]["size"] == wheel.stat().st_size
    assert first["wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert all(len(item["sha256"]) == 64 for item in first["files"])
    assert by_name(first)["synthetic_pkg/core.py"]["size"] == len(DEFAULT_MEMBERS[
        "synthetic_pkg/core.py"])
    assert first["dist_info"] == {
        "name": "synthetic-pkg", "version": "0.1.0",
        "generator": "synthetic-builder (1.0)",
    }
    assert first["supplied_source_commit"] == COMMIT
    assert first["source_commit_verified"] is False
    assert first["manifest_environment"] == {
        "python": platform.python_version(), "zlib": zlib.ZLIB_VERSION,
        "build_environment_proof": False,
    }


def test_cli_prints_identical_sorted_json_for_the_same_wheel(tmp_path):
    wheel = write_wheel(tmp_path / "synthetic-0.1.0.whl")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    command = [sys.executable, str(TOOL), str(wheel), "--source-commit", COMMIT]
    first = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    second = subprocess.run(command, capture_output=True, text=True, env=env, check=False)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == manifest.build_manifest(wheel, source_commit=COMMIT)


def test_one_byte_member_change_changes_only_that_member_digest(tmp_path):
    base = write_wheel(tmp_path / "base.whl")
    changed_members = dict(DEFAULT_MEMBERS)
    changed_members["synthetic_pkg/core.py"] = b"def value():\n    return 3\n"
    changed = write_wheel(tmp_path / "changed.whl", members=changed_members)

    first = manifest.build_manifest(base, source_commit=COMMIT)
    second = manifest.build_manifest(changed, source_commit=COMMIT)
    first_files, second_files = by_name(first), by_name(second)

    assert first["wheel"]["sha256"] != second["wheel"]["sha256"]
    assert first_files["synthetic_pkg/core.py"] != second_files["synthetic_pkg/core.py"]
    assert first_files["synthetic_pkg/core.py"]["size"] == len(
        changed_members["synthetic_pkg/core.py"])
    assert first_files["synthetic_pkg/__init__.py"] == second_files[
        "synthetic_pkg/__init__.py"]
    assert first["dist_info"] == second["dist_info"]


def test_duplicate_zip_names_are_rejected(tmp_path):
    wheel = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            archive.writestr("synthetic_pkg/a.py", b"one")
            archive.writestr("synthetic_pkg/a.py", b"two")
        archive.writestr(f"{DIST}/METADATA", METADATA)

    with pytest.raises(manifest.ManifestError, match="duplicate"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


@pytest.mark.parametrize("name", [
    "../escape.py", "/absolute.py", "pkg\\evil.py", "pkg/../../escape.py", "./pkg/x.py",
])
def test_unsafe_member_names_are_rejected_without_writing(tmp_path, name):
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, b"payload")
        archive.writestr(f"{DIST}/METADATA", METADATA)

    with pytest.raises(manifest.ManifestError, match="unsafe"):
        manifest.build_manifest(wheel, source_commit=COMMIT)
    assert not (tmp_path.parent / "escape.py").exists()


def test_truncated_zip_is_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "whole.whl")
    broken = tmp_path / "truncated.whl"
    broken.write_bytes(wheel.read_bytes()[:60])

    with pytest.raises(manifest.ManifestError, match="ZIP"):
        manifest.build_manifest(broken, source_commit=COMMIT)


def test_member_crc_corruption_is_rejected(tmp_path):
    payload = b"payload-0123456789"
    wheel = write_wheel(tmp_path / "crc.whl", members={"pkg/data.bin": payload},
                        compression=zipfile.ZIP_STORED)
    raw = wheel.read_bytes()
    assert raw.count(payload) == 1
    broken = tmp_path / "crc-bad.whl"
    broken.write_bytes(raw.replace(payload, b"payload-012345678X"))

    with pytest.raises(manifest.ManifestError, match="member"):
        manifest.build_manifest(broken, source_commit=COMMIT)


def test_missing_dist_info_metadata_is_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "missing.whl", metadata=None)

    with pytest.raises(manifest.ManifestError, match="dist-info"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_ambiguous_dist_info_metadata_is_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "ambiguous.whl", extra_metadata=METADATA)

    with pytest.raises(manifest.ManifestError, match="ambiguous"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_duplicate_metadata_headers_are_rejected(tmp_path):
    metadata = METADATA.replace("Version: 0.1.0\n", "Version: 0.1.0\nVersion: 9.9.9\n")
    wheel = write_wheel(tmp_path / "headers.whl", metadata=metadata)

    with pytest.raises(manifest.ManifestError, match="ambiguous"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_missing_required_metadata_is_rejected(tmp_path):
    metadata = METADATA.replace("Version: 0.1.0\n", "")
    wheel = write_wheel(tmp_path / "no-version.whl", metadata=metadata)

    with pytest.raises(manifest.ManifestError, match="missing"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_generator_comes_from_the_dist_info_wheel_file(tmp_path):
    wheel = write_wheel(tmp_path / "realistic.whl")

    report = manifest.build_manifest(wheel, source_commit=COMMIT)
    assert report["dist_info"] == {
        "name": "synthetic-pkg", "version": "0.1.0",
        "generator": "synthetic-builder (1.0)",
    }


def test_missing_wheel_generator_is_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "no-wheel.whl", wheel_file=None)

    with pytest.raises(manifest.ManifestError, match="missing"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_ambiguous_wheel_generator_is_rejected(tmp_path):
    wheel_file = WHEEL_TEXT.replace(
        "Generator: synthetic-builder (1.0)\n", "Generator: one\nGenerator: two\n")
    wheel = write_wheel(tmp_path / "ambiguous-wheel.whl", wheel_file=wheel_file)

    with pytest.raises(manifest.ManifestError, match="ambiguous"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_wheel_size_limit_is_checked_before_reading(tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path / "bounded.whl")
    monkeypatch.setattr(manifest, "MAX_WHEEL_BYTES", 64)

    with pytest.raises(manifest.ManifestError, match="wheel exceeds"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_member_count_limit_is_checked(tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path / "bounded.whl")
    monkeypatch.setattr(manifest, "MAX_MEMBERS", 2)

    with pytest.raises(manifest.ManifestError, match="member count"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_member_size_limit_is_checked_before_decompression(tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path / "bounded.whl", members={"pkg/data.bin": b"x" * 65},
                        metadata="Name: small\nVersion: 1\n\n")
    monkeypatch.setattr(manifest, "MAX_MEMBER_BYTES", 64)

    with pytest.raises(manifest.ManifestError, match="member"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_total_uncompressed_limit_is_checked(tmp_path, monkeypatch):
    wheel = write_wheel(
        tmp_path / "bounded.whl",
        members={"pkg/a.bin": b"a" * 20, "pkg/b.bin": b"b" * 20},
        metadata="Name: small\nVersion: 1\n\n",
    )
    monkeypatch.setattr(manifest, "MAX_TOTAL_BYTES", 50)

    with pytest.raises(manifest.ManifestError, match="total"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


def test_metadata_size_limit_is_checked(tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path / "bounded.whl")
    monkeypatch.setattr(manifest, "MAX_METADATA_BYTES", 10)

    with pytest.raises(manifest.ManifestError, match="METADATA"):
        manifest.build_manifest(wheel, source_commit=COMMIT)


@pytest.mark.parametrize("commit", ["", "0" * 39, "0" * 41, "Z" * 40, "A" * 40])
def test_source_commit_must_be_forty_lowercase_hex(tmp_path, commit):
    wheel = write_wheel(tmp_path / "synthetic-0.1.0.whl")

    with pytest.raises(manifest.ManifestError, match="source commit"):
        manifest.build_manifest(wheel, source_commit=commit)


def test_errors_do_not_echo_wheel_content(tmp_path):
    secret = b"TOPSECRET-CONTENT-123"
    wheel = tmp_path / "secret.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            archive.writestr("synthetic_pkg/a.py", secret)
            archive.writestr("synthetic_pkg/a.py", secret)
        archive.writestr(f"{DIST}/METADATA", METADATA)

    with pytest.raises(manifest.ManifestError) as caught:
        manifest.build_manifest(wheel, source_commit=COMMIT)
    assert "TOPSECRET" not in str(caught.value)

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, str(TOOL), str(wheel),
                             "--source-commit", COMMIT],
                            capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 1
    assert "TOPSECRET" not in result.stderr
