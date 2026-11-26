"""Early run_offline objective directory wiring through the FireRed factory (#139).

Synthetic only: recording doubles replace ROM identification/admission and the game-owned
directory factory. The invented input never reaches geometry, an emulator or a real ROM;
the decider stage raises a synthetic AppError and the emulator sentinel asserts it never opens.
"""

from __future__ import annotations

from pokegent import app

WARNING = "WARNING objective ROM tables unavailable before bridge start: {exc}"


def _run(tmp_path, monkeypatch, calls, *, directory_error=None):
    source = tmp_path / "invented-input"
    source.write_bytes(b"synthetic-rom-placeholder" * 4)
    profiles = object()
    identified = object()
    admitted = object()
    directory = object()
    logs = []

    def load_profiles(*args, **kwargs):
        calls.append(("profiles", args, kwargs))
        return profiles

    def identify_rom_file(*args, **kwargs):
        calls.append(("identify", args, kwargs))
        return identified

    def require_supported(*args, **kwargs):
        calls.append(("admit", args, kwargs))
        return admitted

    def make_objective_directory(*args, **kwargs):
        calls.append(("directory", args, kwargs))
        if directory_error is not None:
            raise directory_error
        return directory

    def build_decider(*args, **kwargs):
        calls.append(("decider", args, kwargs))
        raise app.AppError("synthetic decider refusal")

    def never_open(*args, **kwargs):
        raise AssertionError("emulator must not open")

    monkeypatch.setattr(app, "load_config_file", lambda *_: {})
    monkeypatch.setattr(app, "build_decider", build_decider)
    monkeypatch.setattr(app, "open_emulator", never_open)
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.rom.rom_data.load_profiles", load_profiles
    )
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.rom.rom_data.identify_rom_file", identify_rom_file
    )
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.rom.rom_data.require_supported", require_supported
    )
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.navigation.make_objective_directory",
        make_objective_directory,
        raising=False,
    )

    report = app.run_offline(
        state=source,
        rom_path=source,
        out_dir=tmp_path / "run",
        decider="hybrid",
        log=logs.append,
    )
    return report, logs, source, identified, admitted, directory


def test_early_directory_uses_factory_after_admission_before_decider(
    tmp_path, monkeypatch
):
    calls = []
    report, logs, source, identified, admitted, _ = _run(
        tmp_path, monkeypatch, calls
    )

    assert [call[0] for call in calls] == [
        "profiles", "identify", "admit", "directory", "decider"
    ]
    assert calls[1][1][0] == source
    assert calls[2][1] == (identified, calls[1][1][1])
    assert calls[3][1] == (source.read_bytes(), admitted)
    assert calls[3][2] == {}
    assert not any(line.startswith(WARNING.split("{")[0]) for line in logs)
    assert report.exit_code == app.EXIT_USAGE
    assert report.stop_reason == "synthetic decider refusal"


def test_directory_failure_warns_verbatim_and_continues_to_decider(tmp_path, monkeypatch):
    calls = []
    error = RuntimeError("synthetic directory refusal")
    report, logs, _, _, _, _ = _run(
        tmp_path, monkeypatch, calls, directory_error=error
    )

    assert logs[0] == WARNING.format(exc=error)
    assert [call[0] for call in calls] == [
        "profiles", "identify", "admit", "directory", "decider"
    ]
    assert report.exit_code == app.EXIT_USAGE
    assert report.stop_reason == "synthetic decider refusal"
