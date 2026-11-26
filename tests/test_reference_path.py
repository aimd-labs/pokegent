"""Focused tests for the bundled reference resolver (pokegent/resources.py, #395)."""

from __future__ import annotations

import pytest

from pokegent.resources import reference_path

BUNDLED = "scenes.yaml"


def test_env_override_wins_and_is_returned_unchecked(monkeypatch, tmp_path):
    override = tmp_path / "operator.yaml"
    monkeypatch.setenv("POKEGENT_SCENES", str(override))

    assert reference_path(BUNDLED, env="POKEGENT_SCENES") == override


def test_empty_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv("POKEGENT_SCENES", "")

    resolved = reference_path(BUNDLED, env="POKEGENT_SCENES")
    assert resolved.is_file() and "references" in resolved.parts


def test_bundled_file_resolves_to_real_references_path():
    resolved = reference_path(BUNDLED)

    assert resolved.is_file()
    assert "references" in resolved.parts


def test_missing_name_mentions_the_name_in_error():
    with pytest.raises(FileNotFoundError, match="missing-file.yaml"):
        reference_path("missing-file.yaml")
