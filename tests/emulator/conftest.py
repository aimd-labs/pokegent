"""Local ROM-free actor fixture; no native backend or shared decoder helpers."""

from unittest.mock import Mock

import pytest

from pokegent.emulator import EmulatorActor
from pokegent.testing.fake_emulator import FakeGBAEmulator


@pytest.fixture
def fake(monkeypatch):
    backend = FakeGBAEmulator()
    for name in ("button_down", "press_buttons", "tick"):
        monkeypatch.setattr(backend, name, Mock(wraps=getattr(backend, name)))
    return backend


@pytest.fixture
def actor(fake):
    instance = EmulatorActor(fake, call_timeout_s=3)
    try:
        yield instance
    finally:
        assert instance.close(timeout=3), "actor thread did not stop"
