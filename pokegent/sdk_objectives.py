"""Opt-in local objective facade over one caller-owned, existing ``ObjectiveStore``.

This module performs local store calls only: it provides no independent snapshot provider or new
read path, calls no server, MCP, runtime, executor, backend or game input, and it exposes no raw
file or history export. A delegated ``ObjectiveStore.propose`` may still use that store's own
already-bound fresh snapshot for already-satisfied admission. The supplied selection and store are
trusted caller inputs; they are not cryptographically bound to a plugin ROM, a running process or
a transport, and this facade proves no such binding. Disabled unless the caller explicitly opts in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pokegent.games.plugin import PluginSelection
from pokegent.goals.models import Objective
from pokegent.goals.store import ObjectiveStore, Proposal

#: The existing objective schemas and admission are FireRed-specific in this slice.
SUPPORTED_GAME = "firered"


class ObjectiveSdkRejected(ValueError):
    """The SDK refused construction or an objective request."""


class ObjectiveSdk:
    """Read and propose through one supplied store; no independent authority or validation."""

    def __init__(
        self,
        selection: PluginSelection,
        store: ObjectiveStore,
        *,
        enabled: bool = False,
    ) -> None:
        if not isinstance(selection, PluginSelection):
            raise TypeError("selection must be an admitted PluginSelection")
        if not isinstance(store, ObjectiveStore):
            raise TypeError("store must be a caller-owned ObjectiveStore")
        if type(enabled) is not bool:
            raise TypeError("enabled must be exactly bool")
        if selection.descriptor.game != SUPPORTED_GAME:
            raise ObjectiveSdkRejected(
                f"objective SDK supports the {SUPPORTED_GAME} game namespace only"
            )
        self._store = store
        self._enabled = enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ObjectiveSdkRejected("objective SDK is opt-in; construct it with enabled=True")

    def current(self) -> Objective | None:
        """The store's active typed objective, including its expiry semantics."""
        self._require_enabled()
        return self._store.current()

    def propose(self, data: Mapping[str, Any], *, expected_revision: int) -> Proposal:
        """Forward one copied, agent-bound objective through the store's compare-and-set."""
        self._require_enabled()
        if not isinstance(data, Mapping):
            raise ObjectiveSdkRejected("objective must be a mapping")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ObjectiveSdkRejected("expected_revision must be a plain nonnegative int")
        payload = dict(data)
        payload["set_by"] = "agent"  # payload cannot impersonate an operator or script writer
        return self._store.propose(payload, expected_revision=expected_revision)
