import pytest

from pokegent.user_config import load_user_config


@pytest.mark.parametrize("section", ["system1", "hybrid", "battle", "field", "output"])
@pytest.mark.parametrize("value", [None, "secret-value", ["secret-value"]])
def test_consumed_sections_reject_non_mappings_without_echo(tmp_path, section, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"{section}: {value!r}\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_user_config(path, environ={})
    assert f"{section} must be a mapping" == str(exc.value)
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    ("section", "key"),
    [("system1", "misspelled"), ("hybrid", "misspelled"),
     ("battle", "misspelled"), ("field", "misspelled"), ("output", "misspelled")],
)
def test_consumed_sections_reject_unknown_keys(tmp_path, section, key):
    path = tmp_path / "config.yaml"
    path.write_text(f"{section}:\n  {key}: ignored\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"unknown {section} settings"):
        load_user_config(path, environ={})


def test_valid_consumed_sections_and_environment_cli_precedence(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "system1:\n  model: local\nhybrid:\n  override_window_s: 2\n"
        "battle:\n  opponent_knowledge: fair_play\nfield:\n  menus: false\n"
        "output:\n  run_dir: runs/local\nexternal:\n  port: 19760\n",
        encoding="utf-8",
    )
    config = load_user_config(
        path,
        environ={"POKEGENT_EXTERNAL_PORT": "19761"},
        cli={"external": {"port": 19762}},
    )
    assert config["system1"]["model"] == "local"
    assert config["hybrid"]["override_window_s"] == 2
    assert config["battle"]["opponent_knowledge"] == "fair_play"
    assert config["field"]["menus"] is False
    assert config["output"]["run_dir"] == "runs/local"
    assert config["external"]["port"] == 19762
    env_config = load_user_config(path, environ={"POKEGENT_EXTERNAL_PORT": "19761"})
    assert env_config["external"]["port"] == 19761
    assert isinstance(env_config["external"]["port"], int)


def test_system1_timeout_alias_and_schema_forwarded_fields(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "system1:\n  timeouts: 3\n  max_state_tokens: 2048\n  endpoint_path: /v2/check\n",
        encoding="utf-8",
    )
    from pokegent.user_config import to_legacy_config

    legacy = to_legacy_config(load_user_config(path, environ={}))
    assert legacy["system_one"]["timeout_seconds"] == 3
    assert legacy["system_one"]["max_state_tokens"] == 2048
    assert legacy["system_one"]["endpoint_path"] == "/v2/check"


@pytest.mark.parametrize("value", [None, "3", [3], False, 0])
def test_system1_timeouts_reject_invalid_shapes(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"system1:\n  timeouts: {value!r}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="system1.timeouts"):
        load_user_config(path, environ={})
