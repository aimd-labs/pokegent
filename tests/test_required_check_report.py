"""Synthetic GitHub check-runs exports only; no network, credentials or real branch policy."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import check_required_checks as checks

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_required_checks.py"
HEAD = "a" * 40
OLD_HEAD = "b" * 40


def run(name, sha=HEAD, status="completed", conclusion="success", **extra):
    return {"id": 1, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion, "started_at": "2026-01-01T00:00:00Z", **extra}


def evaluate(rows, required, head=HEAD):
    document = {"total_count": len(rows), "check_runs": rows}
    return checks.evaluate(document, head=head, required=required)


def states(report):
    return {entry["name"]: entry["state"] for entry in report["checks"]}


def cli(tmp_path, document, *args):
    path = tmp_path / "check-runs.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return subprocess.run([sys.executable, "-I", str(TOOL), str(path), *args],
                          capture_output=True, text=True, timeout=30)


def ready_document():
    return {"total_count": 4, "check_runs": [run("docs"), run("lint"), run("unit"), run("windows")]}


def test_three_successes_at_exact_head_are_ready_and_sorted():
    report = evaluate(ready_document()["check_runs"], ["unit", "lint", "docs"])
    assert report["ready"] is True
    assert [entry["name"] for entry in report["checks"]] == ["docs", "lint", "unit"]
    assert set(states(report).values()) == {"success"}
    assert report["head"] == HEAD
    assert report["kind"] == checks.REPORT_KIND
    assert "check evidence only" in report["note"] and "not merge authorization" in report["note"]


def test_old_head_success_is_never_promoted_to_the_exact_head():
    report = evaluate([run("gate", sha=OLD_HEAD)], ["gate"])
    assert report["ready"] is False
    assert states(report) == {"gate": "missing"}


@pytest.mark.parametrize(("row", "state"), [
    (run("gate", status="queued", conclusion=None), "pending"),
    (run("gate", status="in_progress", conclusion=None), "pending"),
    (run("gate", status="completed", conclusion=None), "failed"),
    (run("gate", conclusion="failure"), "failed"),
    (run("gate", conclusion="cancelled"), "failed"),
    (run("gate", conclusion="timed_out"), "failed"),
    (run("gate", conclusion="action_required"), "failed"),
    (run("gate", conclusion="stale"), "failed"),
    (run("gate", conclusion="neutral"), "failed"),
    (run("gate", conclusion="skipped"), "failed"),
])
def test_unfinished_or_unsuccessful_rows_block_readiness(row, state):
    report = evaluate([row], ["gate"])
    assert report["ready"] is False
    assert states(report) == {"gate": state}


def test_missing_check_blocks_while_unrequested_names_are_ignored():
    report = evaluate([run("gate"), run("some-other-app")], ["gate", "docs-only"])
    assert report["ready"] is False
    assert states(report) == {"docs-only": "missing", "gate": "success"}


def test_name_must_match_exactly_without_prefix_or_suffix():
    rows = [run("gate (ubuntu-latest)"), run("x-gate"), run("Gate")]
    report = evaluate(rows, ["gate"])
    assert report["ready"] is False
    assert states(report) == {"gate": "missing"}


def test_duplicate_matching_names_are_ambiguous():
    for rows in ([run("gate"), run("gate")], [run("gate"), run("gate", conclusion="failure")]):
        report = evaluate(rows, ["gate"])
        assert report["ready"] is False
        assert states(report) == {"gate": "duplicate"}


def test_missing_head_sha_on_a_matching_row_is_unknown_not_success():
    row = run("gate")
    del row["head_sha"]
    report = evaluate([row], ["gate"])
    assert report["ready"] is False
    assert states(report) == {"gate": "unknown"}
    both = evaluate([run("gate"), {**row, "id": 2}], ["gate"])
    assert both["ready"] is False
    assert states(both) == {"gate": "unknown"}


def test_repeated_required_names_dedupe():
    report = evaluate([run("gate")], ["gate", "gate"])
    assert report["checks"] == [{"name": "gate", "state": "success"}]


@pytest.mark.parametrize("head", ["", "A" * 40, "a" * 39, "a" * 41, "g" * 40, None, 12])
def test_invalid_head_is_a_typed_input_error(head):
    with pytest.raises(checks.CheckInputError):
        checks.evaluate({"check_runs": []}, head=head, required=["gate"])


def test_empty_or_unbounded_required_lists_are_invalid():
    with pytest.raises(checks.CheckInputError):
        checks.evaluate({"check_runs": []}, head=HEAD, required=[])
    with pytest.raises(checks.CheckInputError):
        checks.evaluate({"check_runs": []}, head=HEAD, required=[""])
    with pytest.raises(checks.CheckInputError):
        checks.evaluate({"check_runs": []}, head=HEAD, required=["n"] * (checks.MAX_REQUIRED + 1))
    with pytest.raises(checks.CheckInputError):
        checks.evaluate({"check_runs": []}, head=HEAD, required=["x" * (checks.NAME_LIMIT + 1)])


@pytest.mark.parametrize("document", [
    [],
    {"check_runs": {}},
    {"check_runs": ["not-a-row"]},
    {"check_runs": [{"status": "completed", "conclusion": "success"}]},
    {"check_runs": [{"name": 7, "status": "completed", "conclusion": None}]},
    {"check_runs": [{"name": "gate", "status": 1, "conclusion": "success"}]},
    {"check_runs": [{"name": "gate", "status": "completed", "conclusion": ["nope"]}]},
    {"check_runs": [{"name": "gate", "head_sha": 3, "status": "completed",
                     "conclusion": "success"}]},
    {"check_runs": [run("x" * (checks.NAME_LIMIT + 1))]},
])
def test_malformed_documents_fail_without_echoing_records(document):
    with pytest.raises(checks.CheckInputError) as caught:
        checks.evaluate(document, head=HEAD, required=["gate"])
    assert "not-a-row" not in str(caught.value)
    assert "nope" not in str(caught.value)
    assert "xxxxx" not in str(caught.value)


@pytest.mark.parametrize("total_count", [None, 3, 5, True, "4", -1, checks.MAX_CHECKS + 1])
def test_total_count_must_be_a_bounded_integer_equal_to_the_rows(total_count):
    document = ready_document()
    if total_count is None:
        del document["total_count"]
    else:
        document["total_count"] = total_count
    with pytest.raises(checks.CheckInputError):
        checks.evaluate(document, head=HEAD, required=["unit", "docs"])


def test_zero_total_count_with_no_rows_reports_required_checks_missing():
    report = checks.evaluate({"total_count": 0, "check_runs": []}, head=HEAD, required=["gate"])
    assert report["ready"] is False
    assert states(report) == {"gate": "missing"}


def test_bounds_are_checked_before_parsing(tmp_path):
    too_many = {"check_runs": [run("gate")] * (checks.MAX_CHECKS + 1)}
    with pytest.raises(checks.CheckInputError):
        checks.evaluate(too_many, head=HEAD, required=["gate"])
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (checks.MAX_FILE_BYTES + 1))
    with pytest.raises(checks.CheckInputError):
        checks.read_document(oversized)


def test_cli_ready_exit_zero_and_deterministic_json(tmp_path):
    first = cli(tmp_path, ready_document(), "--head", HEAD, "--required", "unit",
                "--required", "docs")
    assert first.returncode == 0
    assert first.stderr == ""
    assert json.loads(first.stdout)["ready"] is True
    second = cli(tmp_path, ready_document(), "--head", HEAD, "--required", "docs",
                 "--required", "unit")
    assert second.returncode == 0
    assert first.stdout == second.stdout


def test_cli_not_ready_exit_one(tmp_path):
    document = {"total_count": 1,
                "check_runs": [run("gate", status="in_progress", conclusion=None)]}
    result = cli(tmp_path, document, "--head", HEAD, "--required", "gate")
    assert result.returncode == 1
    assert json.loads(result.stdout)["ready"] is False
    assert result.stderr == ""


def test_cli_unreadable_or_invalid_input_exit_two_without_stdout(tmp_path):
    absent = subprocess.run([sys.executable, "-I", str(TOOL), str(tmp_path / "absent.json"),
                             "--head", HEAD, "--required", "gate"],
                            capture_output=True, text=True, timeout=30)
    assert absent.returncode == 2 and absent.stdout == ""
    assert absent.stderr.startswith("required-check evaluation failed: ")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", str(TOOL), str(broken), "--head", HEAD,
                             "--required", "gate"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and result.stdout == ""
    assert "not json" not in result.stderr


def test_cli_count_mismatch_exit_two_without_echoing_the_count(tmp_path):
    document = ready_document()
    document["total_count"] = 1234
    result = cli(tmp_path, document, "--head", HEAD, "--required", "unit")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("required-check evaluation failed: ")
    assert "1234" not in result.stderr


def test_cli_deeply_nested_json_is_typed_input_error(tmp_path):
    path = tmp_path / "nested.json"
    path.write_bytes(b"[" * 2000 + b"]" * 2000)
    result = subprocess.run([sys.executable, "-I", str(TOOL), str(path), "--head", HEAD,
                             "--required", "gate"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("required-check evaluation failed: ")
    assert "[[[[" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_requires_a_required_name_and_never_echoes_row_values(tmp_path):
    missing_flag = cli(tmp_path, ready_document(), "--head", HEAD)
    assert missing_flag.returncode == 2 and missing_flag.stdout == ""
    document = {"check_runs": [{"name": "secret-marker", "head_sha": HEAD,
                                "status": "completed", "conclusion": ["nope"]}]}
    result = cli(tmp_path, document, "--head", HEAD, "--required", "gate")
    assert result.returncode == 2 and result.stdout == ""
    assert "secret-marker" not in result.stderr and "nope" not in result.stderr


def test_json_integer_limit_exceeded_is_typed_input_error(tmp_path, capsys):
    """JSON with integer exceeding sys.set_int_max_str_digits becomes CheckInputError."""
    # Save and restore the integer limit
    import sys
    original_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        # Create JSON with raw 1000-digit integer (not a string)
        large_int = "1" + "0" * 999
        json_text = (
            '{"total_count": 1, "check_runs": [{"id": 1, "name": "gate", '
            '"head_sha": "'
            + "a" * 40
            + '", "status": "completed", "conclusion": "success", '
            '"started_at": "2026-01-01T00:00:00Z", "big_number": '
            + large_int
            + "}]}"
        )
        path = tmp_path / "bigint.json"
        path.write_text(json_text, encoding="utf-8")

        # read_document should raise CheckInputError, not ValueError
        with pytest.raises(checks.CheckInputError) as caught:
            checks.read_document(path)
        assert "not valid UTF-8 JSON or is too deeply nested" in str(caught.value)

        # CLI (main) should return 2 with no stdout, no traceback
        # Call main directly in the same process with limit=640
        exit_code = checks.main([str(path), "--head", HEAD, "--required", "gate"])
        assert exit_code == 2

        # Capture stdout/stderr
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("required-check evaluation failed: ")
        assert "Traceback" not in captured.err
        assert "1000" not in captured.err  # no raw digits echoed
    finally:
        sys.set_int_max_str_digits(original_limit)


def test_embedded_null_byte_in_path_is_typed_input_error(tmp_path, capsys):
    """Path containing embedded NUL byte raises CheckInputError, not ValueError."""
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(
        '{"total_count": 1, "check_runs": [{"id": 1, "name": "gate", '
        '"head_sha": "'
        + "a" * 40
        + '", "status": "completed", "conclusion": "success", '
        '"started_at": "2026-01-01T00:00:00Z"}]}',
        encoding="utf-8",
    )

    # read_document with embedded NUL in path should raise CheckInputError
    nul_path = str(valid_path) + "\0malicious"
    with pytest.raises(checks.CheckInputError) as caught:
        checks.read_document(nul_path)
    assert "input file is unreadable" in str(caught.value)

    # CLI (main) should return 2 with no stdout, no traceback
    exit_code = checks.main([nul_path, "--head", HEAD, "--required", "gate"])
    assert exit_code == 2

    # Capture stdout/stderr
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("required-check evaluation failed: ")
    assert "Traceback" not in captured.err
    assert "embedded null" not in captured.err.lower()  # no raw error echoed
