"""Conformance checks for the supported systemd (or process-manager) restart policy.

Issue #33: an interrupted segment whose runner never publishes a successor leaves
live-run.json with status=blocked, and the supervisor exits 4 ("previous run is
blocked"). Without a bounded restart policy a process manager blindly retrying on
failure spun the empty-port unit ~29x. This test locks the *supported* unit policy
(ops/systemd/pokegent-live-dropin.conf) against the supervisor's owner-review exit
contract so a later edit cannot silently unbind the restart limit.
"""
import re
from pathlib import Path

from pokegent.supervisor import Supervisor

OPS_UNIT_DIR = Path(__file__).resolve().parents[1] / "ops" / "systemd"
DROPIN = OPS_UNIT_DIR / "pokegent-live-dropin.conf"


def _value(section, key):
    text = DROPIN.read_text()
    # crude but fine for a two-section ini-style drop-in
    match = re.search(rf"^\s*{key}\s*=\s*(.+)$", text, re.MULTILINE)
    assert match, f"{key} not present in {DROPIN}"
    return match.group(1).strip().rstrip('"')


def test_dropin_bounds_restart_spin():
    """The supported drop-in must bound auto-restarts (issue #33): a bounded burst in
    the interval plus Restart=on-failure, exactly so an empty-port blocked loop cannot
    exceed a handful of attempts."""
    assert _value("Unit", "StartLimitIntervalSec") == "600"
    assert _value("Unit", "StartLimitBurst") == "3"
    assert _value("Service", "Restart") in ("on-failure", "on-abnormal")
    assert _value("Service", "RestartSec").isdigit() or float(_value("Service", "RestartSec")) >= 0


def test_dropin_does_not_auto_revive_owner_review_exits():
    """The owner-review exits (3 = verdict owner_review, 4 = blocked/crash-limit) must
    be RestartPreventExitStatus so a manager cannot auto-revive into the same blocked
    record. systemd wants whitespace-separated values ('3 4 78'); assert all are
    covered."""
    prevented = {int(x) for x in _value("Service", "RestartPreventExitStatus").split()}
    assert {3, 4, 78}.issubset(prevented)


def test_dropin_restart_prevent_exit_is_whitespace_separated():
    """systemd rejects comma-separated RestartPreventExitStatus ('3,4,78' is ignored with
    a verify warning), so the supported drop-in must use the whitespace form '3 4 78'."""
    raw = _value("Service", "RestartPreventExitStatus")
    assert "," not in raw, "RestartPreventExitStatus must be whitespace-separated for systemd"
    tokens = raw.split()
    assert len(tokens) == 3 and all(t.isdigit() for t in tokens)


def test_supervisor_exit_codes_match_prevent_list():
    """The exit codes the supervisor actually returns for owner-review/blocked must be
    covered by the drop-in's RestartPreventExitStatus, so the documented unit policy and
    the code contract stay in lockstep."""
    # Re-derive the non-zero exits Supervisor.run() can return for review decisions.
    # 3 == owner_review, 4 == blocked/crash-limit (see supervisor.run).
    prevented = {int(x) for x in _value("Service", "RestartPreventExitStatus").split()}
    assert Supervisor.exit_owner_review in prevented
    assert Supervisor.exit_blocked in prevented
