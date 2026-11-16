"""One call for the field family's ``register``: the Surf executors and verifiers."""

from __future__ import annotations

from pokegent.actions._exec.registry import ExecutorSpec, VerifierSpec
from pokegent.actions.field.attempts import AttemptLedger
from pokegent.actions.field.surf import macros, verify


def executor_specs(ledger: AttemptLedger | None = None) -> list[ExecutorSpec]:
    return macros.executor_specs(ledger)


def verifier_specs() -> list[VerifierSpec]:
    return verify.verifier_specs()
