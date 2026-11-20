"""Invalid external config shapes fail before runtime; valid values keep their precedence."""

import sys

import pytest

from pokegent import cli
from pokegent.user_config import load_user_config


def write_config(tmp_path, text):
    path = tmp_path / "pokegent.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "section",
    ("external:\n", "external: 19760\n", "external: [19760]\n", "external: enabled\n"),
)
def test_external_section_must_be_a_mapping(tmp_path, section):
    with pytest.raises(ValueError, match="^external must be a mapping$"):
        load_user_config(write_config(tmp_path, section), environ={})


def test_unknown_external_key_is_rejected_by_name_only(tmp_path):
    path = write_config(tmp_path, "external:\n  prot: 19761\n")
    with pytest.raises(ValueError) as error:
        load_user_config(path, environ={})
    message = str(error.value)
    assert "unknown external settings" in message and "prot" in message
    assert "19761" not in message


def test_removed_external_timeout_keeps_dedicated_error(tmp_path):
    path = write_config(tmp_path, "external:\n  timeout: 5\n")
    with pytest.raises(ValueError, match="external.timeout was removed"):
        load_user_config(path, environ={})


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("19699", "external.port must be between 19700 and 19799"),
        ("19800", "external.port must be between 19700 and 19799"),
        ("19760.5", "external.port must be an integer"),
        ('"19760"', "external.port must be an integer"),
        ("true", "external.port must be an integer"),
        ("null", "external.port must be an integer"),
    ),
)
def test_invalid_external_port_is_rejected_without_echo(tmp_path, value, expected):
    path = write_config(tmp_path, f"external:\n  port: {value}\n")
    with pytest.raises(ValueError) as error:
        load_user_config(path, environ={})
    message = str(error.value)
    assert message == expected
    assert value.strip('"') not in message


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0", "external.walk_step_limit must be >= 1"),
        ("-2", "external.walk_step_limit must be >= 1"),
        ("1.5", "external.walk_step_limit must be an integer"),
        ('"8"', "external.walk_step_limit must be an integer"),
        ("true", "external.walk_step_limit must be an integer"),
        ("null", "external.walk_step_limit must be an integer"),
    ),
)
def test_invalid_walk_step_limit_is_rejected_without_echo(tmp_path, value, expected):
    path = write_config(tmp_path, f"external:\n  walk_step_limit: {value}\n")
    with pytest.raises(ValueError) as error:
        load_user_config(path, environ={})
    message = str(error.value)
    assert message == expected
    assert value.strip('"') not in message


@pytest.mark.parametrize(
    "name", ("POKEGENT_EXTERNAL_PORT", "POKEGENT_EXTERNAL_WALK_STEP_LIMIT")
)
def test_malformed_external_environment_is_a_field_error(tmp_path, name):
    with pytest.raises(ValueError) as error:
        load_user_config(tmp_path / "absent.yaml", environ={name: "not-a-number"})
    assert "external." in str(error.value)
    assert "not-a-number" not in str(error.value)


def test_defaults_and_valid_file_values_are_unchanged(tmp_path):
    defaults = load_user_config(tmp_path / "absent.yaml", environ={})
    assert defaults["external"] == {"port": 19760, "walk_step_limit": 8}
    configured = load_user_config(
        write_config(tmp_path, "external: {port: 19770, walk_step_limit: 3}\n"), environ={}
    )
    assert configured["external"] == {"port": 19770, "walk_step_limit": 3}


def test_file_env_cli_precedence_for_external_values(tmp_path):
    path = write_config(tmp_path, "external: {port: 19771, walk_step_limit: 4}\n")
    from_env = load_user_config(
        path,
        environ={
            "POKEGENT_EXTERNAL_PORT": "19772",
            "POKEGENT_EXTERNAL_WALK_STEP_LIMIT": "5",
        },
    )
    assert from_env["external"] == {"port": 19772, "walk_step_limit": 5}
    from_cli = load_user_config(
        path,
        environ={"POKEGENT_EXTERNAL_PORT": "19772"},
        cli={"external": {"port": 19773}},
    )
    assert from_cli["external"] == {"port": 19773, "walk_step_limit": 4}


def test_cli_rejects_bad_external_config_without_runtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pokegent.app.run_offline", lambda **kwargs: sys.exit("ran runtime"))
    monkeypatch.setenv("POKEGENT_FIELD_MENU", "1")
    path = write_config(tmp_path, "external:\n  prot: 19761\n")
    with pytest.raises(SystemExit) as exit:
        cli.main(["run", "--new-game", "--config", str(path), "--out", str(tmp_path / "out")])
    assert exit.value.code == 2
    captured = capsys.readouterr()
    assert "unknown external settings" in captured.err and "prot" in captured.err
    assert "Traceback" not in captured.err and "19761" not in captured.err
