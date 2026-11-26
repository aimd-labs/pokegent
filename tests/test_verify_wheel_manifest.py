"""Synthetic offline wheel-vs-manifest verification; no build, extraction or network."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tools import verify_wheel_manifest as verify
from tools import wheel_manifest as manifest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
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
MEMBERS = {
    "synthetic_pkg/__init__.py": b"VALUE = 1\n",
    "synthetic_pkg/core.py": b"def value():\n    return 2\n",
}


def write_wheel(path, members=None, metadata=METADATA, wheel_file=WHEEL_TEXT):
    members = MEMBERS if members is None else members
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if metadata is not None:
            archive.writestr(f"{DIST}/METADATA", metadata)
        if wheel_file is not None:
            archive.writestr(f"{DIST}/WHEEL", wheel_file)
    return Path(path)


def build_reference(tmp_path, wheel, *, commit=COMMIT):
    data = manifest.build_manifest(wheel, source_commit=commit)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return path, data


def write_reference(path, data):
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return path


def run_cli(wheel, reference, commit):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    command = [
        sys.executable, "-m", "tools.verify_wheel_manifest",
        str(wheel), str(reference), "--source-commit", commit,
    ]
    return subprocess.run(command, capture_output=True, text=True, env=env,
                          cwd=ROOT, check=False)


def test_identical_artifact_matches_and_inputs_are_unchanged(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)
    wheel_before, reference_before = wheel.read_bytes(), reference.read_bytes()

    result = verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)

    assert result["matches"] is True
    assert result["mismatches"] == []
    assert "caller-supplied" in result["scope"]
    assert "no trusted-source" in result["scope"]
    assert wheel.read_bytes() == wheel_before
    assert reference.read_bytes() == reference_before


def test_one_member_byte_change_reports_bounded_field_names(tmp_path):
    wheel = write_wheel(tmp_path / "base.whl")
    reference, _ = build_reference(tmp_path, wheel)
    members = dict(MEMBERS)
    members["synthetic_pkg/core.py"] = b"def value():\n    return 3\n"
    changed = write_wheel(tmp_path / "changed.whl", members=members)

    result = verify.verify_wheel_manifest(changed, reference, source_commit=COMMIT)

    assert result["matches"] is False
    assert result["mismatches"] == ["wheel.sha256", "files"]


def test_metadata_change_reports_dist_info_field(tmp_path):
    wheel = write_wheel(tmp_path / "base.whl")
    reference, _ = build_reference(tmp_path, wheel)
    metadata = METADATA.replace("Version: 0.1.0", "Version: 0.2.0")
    changed = write_wheel(tmp_path / "metadata.whl", metadata=metadata)

    result = verify.verify_wheel_manifest(changed, reference, source_commit=COMMIT)

    assert result["matches"] is False
    assert set(result["mismatches"]) == {
        "wheel.sha256", "files", "dist_info.version",
    }


def test_missing_member_is_a_mismatch_not_an_error(tmp_path):
    wheel = write_wheel(tmp_path / "base.whl")
    reference, _ = build_reference(tmp_path, wheel)
    members = dict(MEMBERS)
    del members["synthetic_pkg/core.py"]
    changed = write_wheel(tmp_path / "missing.whl", members=members)

    result = verify.verify_wheel_manifest(changed, reference, source_commit=COMMIT)

    assert result["matches"] is False
    assert "files" in result["mismatches"]
    assert "wheel.sha256" in result["mismatches"]


def test_caller_expected_commit_difference_has_no_source_trust_claim(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel, commit=COMMIT)
    assert data["source_commit_verified"] is False

    result = verify.verify_wheel_manifest(wheel, reference, source_commit=OTHER_COMMIT)

    assert result["matches"] is False
    assert result["mismatches"] == ["supplied_source_commit"]
    assert "no trusted-source" in result["scope"]


def test_reference_runtime_labels_do_not_change_the_verdict(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    data["manifest_environment"]["python"] = "9.9.9-other"
    data["manifest_environment"]["zlib"] = "0.0-other"
    write_reference(reference, data)

    result = verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)

    assert result["matches"] is True
    assert result["mismatches"] == []


def test_null_generator_matches(tmp_path):
    wheel_file = WHEEL_TEXT.replace("Generator: synthetic-builder (1.0)\n", "")
    wheel = write_wheel(tmp_path / "pkg.whl", wheel_file=wheel_file)
    reference, data = build_reference(tmp_path, wheel)
    assert data["dist_info"]["generator"] is None

    result = verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)

    assert result["matches"] is True


def test_missing_generator_field_is_invalid_while_null_matches(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    data["dist_info"].pop("generator")
    write_reference(reference, data)

    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


def test_reference_row_order_must_match_exactly(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    data["files"].reverse()
    write_reference(reference, data)

    result = verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)

    assert result["matches"] is False
    assert result["mismatches"] == ["files"]


@pytest.mark.parametrize("path,value", [
    (("tool",), "other_tool"),
    (("version",), 2),
    (("version",), True),
    (("source_commit_verified",), True),
    (("wheel", "size"), True),
    (("wheel", "sha256"), "A" * 64),
    (("manifest_environment", "build_environment_proof"), True),
    (("supplied_source_commit",), "not-a-commit"),
])
def test_invalid_reference_fields_are_typed_errors(tmp_path, path, value):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    write_reference(reference, data)

    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


@pytest.mark.parametrize("field", [
    "tool", "version", "wheel", "files", "dist_info",
    "supplied_source_commit", "source_commit_verified", "manifest_environment",
])
def test_missing_reference_root_field_is_a_typed_error(tmp_path, field):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    del data[field]
    write_reference(reference, data)

    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


def test_duplicate_reference_member_names_are_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, data = build_reference(tmp_path, wheel)
    data["files"].append(copy.deepcopy(data["files"][0]))
    write_reference(reference, data)

    with pytest.raises(verify.VerifyError, match="duplicate member"):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


def test_non_object_reference_root_is_rejected(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference = tmp_path / "root.json"
    reference.write_text("[]", encoding="utf-8")

    with pytest.raises(verify.VerifyError, match="object"):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


def test_malformed_deep_and_oversized_manifests_are_typed_errors(
        tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path / "pkg.whl")
    secret = "TOPSECRET-REFERENCE-CONTENT"
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"' + secret + '": ', encoding="utf-8")
    with pytest.raises(verify.VerifyError) as caught:
        verify.verify_wheel_manifest(wheel, malformed, source_commit=COMMIT)
    assert secret not in str(caught.value)

    deep = tmp_path / "deep.json"
    deep.write_text('{"a":' * 3000 + "1" + "}" * 3000, encoding="utf-8")
    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest(wheel, deep, source_commit=COMMIT)

    reference, _ = build_reference(tmp_path, wheel)
    monkeypatch.setattr(verify, "MAX_MANIFEST_BYTES", 16)
    with pytest.raises(verify.VerifyError, match="exceeds"):
        verify.verify_wheel_manifest(wheel, reference, source_commit=COMMIT)


def test_missing_reference_file_is_a_typed_error(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")

    with pytest.raises(verify.VerifyError, match="cannot read"):
        verify.verify_wheel_manifest(
            wheel, tmp_path / "absent.json", source_commit=COMMIT)


def test_embedded_nul_reference_path_is_a_typed_error(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)
    wheel_before, reference_before = wheel.read_bytes(), reference.read_bytes()

    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest(wheel, "bad\x00reference.json",
                                     source_commit=COMMIT)

    assert wheel.read_bytes() == wheel_before
    assert reference.read_bytes() == reference_before


def test_embedded_nul_wheel_path_is_a_typed_error(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)
    wheel_before, reference_before = wheel.read_bytes(), reference.read_bytes()

    with pytest.raises(verify.VerifyError):
        verify.verify_wheel_manifest("bad\x00wheel.whl", reference,
                                     source_commit=COMMIT)

    assert wheel.read_bytes() == wheel_before
    assert reference.read_bytes() == reference_before


def test_main_returns_two_for_embedded_nul_paths_without_traceback(
        tmp_path, capsys):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)

    for argv in ([str(wheel), "bad\x00reference.json",
                  "--source-commit", COMMIT],
                 ["bad\x00wheel.whl", str(reference),
                  "--source-commit", COMMIT]):
        assert verify.main(argv) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("verify_wheel_manifest: ")
        assert "Traceback" not in captured.err


def test_corrupt_and_unsafe_wheels_delegate_to_the_manifest_bounds(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)

    broken = tmp_path / "broken.whl"
    broken.write_bytes(wheel.read_bytes()[:60])
    with pytest.raises(verify.VerifyError, match="ZIP"):
        verify.verify_wheel_manifest(broken, reference, source_commit=COMMIT)

    unsafe = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.py", b"payload")
        archive.writestr(f"{DIST}/METADATA", METADATA)
    with pytest.raises(verify.VerifyError, match="unsafe"):
        verify.verify_wheel_manifest(unsafe, reference, source_commit=COMMIT)


def test_module_cli_exit_codes_determinism_and_inputs(tmp_path):
    wheel = write_wheel(tmp_path / "pkg.whl")
    reference, _ = build_reference(tmp_path, wheel)
    wheel_before, reference_before = wheel.read_bytes(), reference.read_bytes()

    first = run_cli(wheel, reference, COMMIT)
    second = run_cli(wheel, reference, COMMIT)

    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == verify.verify_wheel_manifest(
        wheel, reference, source_commit=COMMIT)
    assert wheel.read_bytes() == wheel_before
    assert reference.read_bytes() == reference_before

    mismatch = run_cli(wheel, reference, OTHER_COMMIT)
    assert mismatch.returncode == 1
    assert json.loads(mismatch.stdout)["matches"] is False

    bad = tmp_path / "bad.json"
    bad.write_text('{"TOPSECRET": ', encoding="utf-8")
    invalid = run_cli(wheel, bad, COMMIT)
    assert invalid.returncode == 2
    assert "TOPSECRET" not in invalid.stderr

    missing = run_cli(wheel, tmp_path / "absent.json", COMMIT)
    assert missing.returncode == 2

    bad_commit = run_cli(wheel, reference, "not-a-commit")
    assert bad_commit.returncode == 2
