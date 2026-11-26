"""Synthetic contract tests for opt-in exact-ROM plugin selection (issue #139)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

import pokegent.games.plugin as plugin_module
from pokegent.games.plugin import (
    MAX_REQUIRED_CAPABILITIES,
    MAX_ROM_BYTES,
    GamePluginDescriptor,
    PluginRefusal,
    PythonGamePluginProtocol,
    RefusalCode,
    ReviewedPluginRegistry,
)

ROM_A = b"synthetic-plugin-rom-alpha" * 8
ROM_B = b"synthetic-plugin-rom-bravo" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@dataclass
class SyntheticServices:
    observed_scene: str


@dataclass
class SyntheticPlugin:
    descriptor: GamePluginDescriptor
    observed_scene: str
    compose_calls: int = 0

    def compose(self) -> SyntheticServices:
        self.compose_calls += 1
        return SyntheticServices(self.observed_scene)


def _plugin(
    *,
    game: str,
    profile_id: str,
    data: bytes,
    scene: str,
    contract_version: int = 1,
    capabilities: frozenset[str] = frozenset({"observation"}),
) -> SyntheticPlugin:
    return SyntheticPlugin(
        GamePluginDescriptor(
            plugin_id=f"{game}:{profile_id}",
            game=game,
            profile_id=profile_id,
            contract_version=contract_version,
            rom_sha1=_digest(data),
            capabilities=capabilities,
        ),
        scene,
    )


def _registry(*plugins: SyntheticPlugin, enabled: bool = True) -> ReviewedPluginRegistry:
    registry = ReviewedPluginRegistry(enabled=enabled)
    for plugin in plugins:
        registry.register(plugin)
    return registry


def _select(
    registry: ReviewedPluginRegistry,
    plugin: SyntheticPlugin,
    data: bytes,
    *,
    required_capabilities: tuple[str, ...] = (),
):
    return registry.select(
        rom_bytes=data,
        game=plugin.descriptor.game,
        profile_id=plugin.descriptor.profile_id,
        required_capabilities=required_capabilities,
    )


def test_registry_defaults_off_and_refuses_before_compose():
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin, enabled=False)
    assert isinstance(plugin, PythonGamePluginProtocol)

    with pytest.raises(PluginRefusal) as caught:
        _select(registry, plugin, ROM_A)

    assert caught.value.code is RefusalCode.DISABLED
    assert plugin.compose_calls == 0


@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
def test_registry_enabled_flag_must_be_exactly_bool(enabled):
    with pytest.raises(TypeError):
        ReviewedPluginRegistry(enabled=enabled)


def test_exact_identity_selects_each_plugin_and_its_services():
    alpha = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha scene")
    bravo = _plugin(game="synthetic-b", profile_id="b-1", data=ROM_B, scene="bravo scene")
    registry = _registry(alpha, bravo)

    selected_alpha = _select(registry, alpha, ROM_A, required_capabilities=("observation",))
    selected_bravo = _select(registry, bravo, ROM_B)

    assert selected_alpha.descriptor is alpha.descriptor
    assert selected_alpha.services.observed_scene == "alpha scene"
    assert selected_bravo.descriptor is bravo.descriptor
    assert selected_bravo.services.observed_scene == "bravo scene"
    assert (alpha.compose_calls, bravo.compose_calls) == (1, 1)


def test_unknown_digest_fails_closed_without_compose():
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin)

    with pytest.raises(PluginRefusal) as caught:
        _select(registry, plugin, ROM_A + b"unknown")

    assert caught.value.code is RefusalCode.UNKNOWN_IDENTITY
    assert plugin.compose_calls == 0


@pytest.mark.parametrize("field", ["game", "profile_id"])
def test_exact_requested_owner_mismatch_fails_closed(field):
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin)
    requested = {
        "rom_bytes": ROM_A,
        "game": plugin.descriptor.game,
        "profile_id": plugin.descriptor.profile_id,
    }
    requested[field] = "different"

    with pytest.raises(PluginRefusal) as caught:
        registry.select(**requested)

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert plugin.compose_calls == 0


def test_unsupported_contract_version_fails_closed():
    plugin = _plugin(
        game="synthetic-a",
        profile_id="a-1",
        data=ROM_A,
        scene="alpha",
        contract_version=2,
    )
    registry = _registry(plugin)

    with pytest.raises(PluginRefusal) as caught:
        _select(registry, plugin, ROM_A)

    assert caught.value.code is RefusalCode.UNSUPPORTED_CONTRACT
    assert plugin.compose_calls == 0


def test_missing_required_capability_fails_closed():
    plugin = _plugin(
        game="synthetic-a",
        profile_id="a-1",
        data=ROM_A,
        scene="alpha",
        capabilities=frozenset({"observation"}),
    )
    registry = _registry(plugin)

    with pytest.raises(PluginRefusal) as caught:
        _select(registry, plugin, ROM_A, required_capabilities=("input",))

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert plugin.compose_calls == 0


@pytest.mark.parametrize("collision", ["digest", "profile"])
def test_duplicate_identity_or_profile_ownership_is_rejected(collision):
    first = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(first)
    if collision == "digest":
        second = _plugin(game="synthetic-b", profile_id="b-1", data=ROM_A, scene="bravo")
    else:
        second = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_B, scene="bravo")

    with pytest.raises(ValueError):
        registry.register(second)

    assert _select(registry, first, ROM_A).services.observed_scene == "alpha"


def test_caller_buffer_is_hashed_without_mutation():
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin)
    caller_buffer = bytearray(ROM_A)

    _select(registry, plugin, caller_buffer)

    assert bytes(caller_buffer) == ROM_A


def test_bounded_inputs_are_rejected_before_compose():
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin)

    with pytest.raises(ValueError):
        _select(registry, plugin, b"\0" * (MAX_ROM_BYTES + 1))
    with pytest.raises(ValueError):
        _select(
            registry,
            plugin,
            ROM_A,
            required_capabilities=("observation",) * (MAX_REQUIRED_CAPABILITIES + 1),
        )

    assert plugin.compose_calls == 0


def test_memoryview_bound_counts_bytes_not_elements(monkeypatch):
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    registry = _registry(plugin)
    view = memoryview(ROM_A).cast("I")
    assert len(view) < view.nbytes
    monkeypatch.setattr(plugin_module, "MAX_ROM_BYTES", len(view))

    with pytest.raises(ValueError):
        _select(registry, plugin, view)

    assert plugin.compose_calls == 0


@pytest.mark.parametrize("contract_version", [True, 1.0, "1"])
def test_descriptor_contract_version_must_be_plain_int(contract_version):
    with pytest.raises(ValueError):
        GamePluginDescriptor(
            plugin_id="synthetic-a:a-1",
            game="synthetic-a",
            profile_id="a-1",
            contract_version=contract_version,
            rom_sha1=_digest(ROM_A),
        )


def test_descriptor_is_immutable():
    plugin = _plugin(game="synthetic-a", profile_id="a-1", data=ROM_A, scene="alpha")
    with pytest.raises((AttributeError, TypeError)):
        plugin.descriptor.game = "changed"
