"""Bounded typed plugin metadata admission (#139).

Synthetic metadata only: no ROM, provider, native emulator or network input.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

import pokegent.games.plugin as plugin_module
from pokegent.games.plugin import (
    MAX_IDENTIFIER_CHARS,
    MAX_REQUIRED_CAPABILITIES,
    GamePluginDescriptor,
    ReviewedPluginRegistry,
)

ROM = b"synthetic-plugin-metadata" * 8


def _digest(data: bytes = ROM) -> str:
    return hashlib.sha1(data).hexdigest()


def _descriptor(**overrides):
    values = dict(
        plugin_id="firered:firered-us-rev0",
        game="firered",
        profile_id="firered-us-rev0",
        contract_version=1,
        rom_sha1=_digest(),
        capabilities=frozenset({"observation"}),
    )
    values.update(overrides)
    return GamePluginDescriptor(**values)


class _BoundedProbe:
    """Finite iterable that fails if a consumer asks past its budget."""

    def __init__(self, items, *, fail_after):
        self._items = tuple(items)
        self._fail_after = fail_after
        self.consumed = 0

    def __iter__(self):
        for item in self._items:
            if self.consumed >= self._fail_after:
                raise AssertionError("consumer read past the bounded capability budget")
            self.consumed += 1
            yield item


@dataclass
class _Services:
    scene: str


@dataclass
class _Plugin:
    descriptor: GamePluginDescriptor
    compose_calls: int = 0

    def compose(self) -> _Services:
        self.compose_calls += 1
        return _Services("synthetic-scene")


def _registry(*, capabilities=frozenset({"observation", "reader"})):
    plugin = _Plugin(_descriptor(capabilities=capabilities))
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    return plugin, registry


def _select(registry, plugin, **overrides):
    values = dict(
        rom_bytes=ROM,
        game=plugin.descriptor.game,
        profile_id=plugin.descriptor.profile_id,
    )
    values.update(overrides)
    return registry.select(**values)


@pytest.mark.parametrize("field", ["plugin_id", "game", "profile_id"])
@pytest.mark.parametrize("value", [1, True, " ", "\t\n", "x" * (MAX_IDENTIFIER_CHARS + 1)])
def test_descriptor_rejects_non_exact_blank_or_overlong_identifiers(field, value):
    with pytest.raises(ValueError):
        _descriptor(**{field: value})


@pytest.mark.parametrize("capabilities", ["observation", b"observation", bytearray(b"x")])
def test_descriptor_rejects_string_containers_as_capabilities(capabilities):
    with pytest.raises(ValueError):
        _descriptor(capabilities=capabilities)


def test_descriptor_bounds_capability_consumption_at_seventeen():
    probe = _BoundedProbe([f"cap-{index}" for index in range(40)], fail_after=17)

    with pytest.raises(ValueError):
        _descriptor(capabilities=probe)

    assert probe.consumed == 17


@pytest.mark.parametrize("capability", [1, "", " ", "x" * (MAX_IDENTIFIER_CHARS + 1)])
def test_descriptor_rejects_malformed_capability_names(capability):
    with pytest.raises(ValueError):
        _descriptor(capabilities=(capability,))


@pytest.mark.parametrize("rom_sha1", [1, True, None, b"0" * 40, bytearray(b"0" * 40)])
def test_descriptor_rejects_non_string_digest_as_value_error(rom_sha1):
    with pytest.raises(ValueError):
        _descriptor(rom_sha1=rom_sha1)


def test_descriptor_accepts_bounded_identifiers_and_finite_iterables():
    descriptor = _descriptor(
        plugin_id="x" * MAX_IDENTIFIER_CHARS,
        game="firered",
        profile_id="firered-us-rev0",
        capabilities=iter(["reader", "reader", "observation"]),
    )

    assert descriptor.capabilities == frozenset({"reader", "observation"})


@pytest.mark.parametrize("contract_version", [True, False])
def test_bool_contract_version_is_still_rejected(contract_version):
    with pytest.raises(ValueError):
        _descriptor(contract_version=contract_version)


@pytest.mark.parametrize("field", ["game", "profile_id"])
@pytest.mark.parametrize("value", [1, True, " ", "x" * (MAX_IDENTIFIER_CHARS + 1)])
def test_select_rejects_bad_identifiers_before_hashing_and_compose(field, value, monkeypatch):
    plugin, registry = _registry()
    hashed: list[bytes] = []
    real_sha1 = hashlib.sha1

    def recording_sha1(data):
        hashed.append(bytes(data))
        return real_sha1(data)

    monkeypatch.setattr(plugin_module.hashlib, "sha1", recording_sha1)

    with pytest.raises(ValueError):
        _select(registry, plugin, **{field: value})

    assert hashed == []
    assert plugin.compose_calls == 0


@pytest.mark.parametrize("required", ["observation", b"observation", 7])
def test_select_rejects_string_or_non_iterable_required_capabilities(required):
    plugin, registry = _registry()

    with pytest.raises(ValueError):
        _select(registry, plugin, required_capabilities=required)

    assert plugin.compose_calls == 0


def test_select_bounds_required_capability_consumption_and_skips_compose():
    plugin, registry = _registry()
    probe = _BoundedProbe([f"cap-{index}" for index in range(40)], fail_after=17)

    with pytest.raises(ValueError):
        _select(registry, plugin, required_capabilities=probe)

    assert probe.consumed == 17
    assert plugin.compose_calls == 0


@pytest.mark.parametrize("capability", [1, "", " ", "x" * (MAX_IDENTIFIER_CHARS + 1)])
def test_select_rejects_malformed_required_capability_names(capability):
    plugin, registry = _registry()

    with pytest.raises(ValueError):
        _select(registry, plugin, required_capabilities=(capability,))

    assert plugin.compose_calls == 0


def test_select_still_accepts_exact_rom_finite_capability_generator():
    plugin, registry = _registry()

    selection = registry.select(
        rom_bytes=ROM,
        game="firered",
        profile_id="firered-us-rev0",
        required_capabilities=iter(["reader", "observation", "reader"]),
    )

    assert selection.descriptor is plugin.descriptor
    assert selection.services.scene == "synthetic-scene"
    assert plugin.compose_calls == 1
    assert MAX_REQUIRED_CAPABILITIES == 16
