import subprocess

from tools.check_public_startup_latency import run_with_deadline


def test_deadline_failure_is_reported_without_waiting():
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="partial")

    code, elapsed, stdout, stderr = run_with_deadline(["synthetic"], timeout=30, runner=timed_out)

    assert code == 124
    assert elapsed < 1
    assert stdout == "partial"
    assert stderr == ""
