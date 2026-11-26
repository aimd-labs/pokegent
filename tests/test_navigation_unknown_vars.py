"""Unknown vars must not satisfy a predicate the implicit zero would make true."""

from pokegent.games.gen3.firered.reader.schemas import Health, ReadError, StateSnapshot
from pokegent.navigation.capabilities import from_snapshot, requirement
from pokegent.navigation.models import CapabilityState, NavigationEdge, NavigationNode

VAR = 0x4001


def _missing_vars_snapshot() -> StateSnapshot:
    """A validated snapshot whose vars read failed, so no var table is attached."""
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=1,
        emulator_frame=0,
        health=Health(
            valid=False,
            read_errors=(
                ReadError(
                    subsystem="vars",
                    code="unreadable",
                    message="synthetic vars read error",
                ),
            ),
        ),
    )


def test_validated_missing_vars_snapshot_is_unknown():
    capabilities = from_snapshot(_missing_vars_snapshot())

    assert capabilities.vars_known is False
    assert capabilities.vars == ()


def test_unknown_vars_never_satisfy_a_zero_default_var_predicate():
    capabilities = from_snapshot(_missing_vars_snapshot())

    assert capabilities.has(f"var:{VAR:#x}:eq:0") is False
    assert requirement(f"var:{VAR:#x}:0", capabilities).met is False
    assert requirement(f"var:{VAR:#x}:lt:1", capabilities).met is False


def test_known_sparse_vars_keep_zero_semantics():
    capabilities = CapabilityState(vars=(), vars_known=True)

    assert requirement(f"var:{VAR:#x}:eq:0", capabilities).met is True
    assert requirement(f"var:{VAR:#x}:lt:1", capabilities).met is True
    assert requirement(f"var:{VAR:#x}:gt:0", capabilities).met is False


def test_known_nonzero_vars_keep_comparators():
    capabilities = CapabilityState(vars=((VAR, 5),))

    assert requirement(f"var:{VAR:#x}:eq:5", capabilities).met is True
    assert requirement(f"var:{VAR:#x}:ge:5", capabilities).met is True
    assert requirement(f"var:{VAR:#x}:lt:1", capabilities).met is False


def test_unknown_vars_keep_non_var_facts_and_conjunctions_fail_closed():
    unknown = CapabilityState(capabilities=frozenset({"surf"}), vars_known=False)
    known = CapabilityState(capabilities=frozenset({"surf"}), vars=(), vars_known=True)

    assert requirement("surf", unknown).met is True
    assert requirement(f"surf&var:{VAR:#x}:eq:0", unknown).met is False
    assert requirement(f"surf&var:{VAR:#x}:eq:0", known).met is True


def test_unknown_vars_leave_a_guarded_edge_unavailable():
    capabilities = from_snapshot(_missing_vars_snapshot())
    origin = NavigationNode(0, 0, 1, 1)
    edge = NavigationEdge(
        stable_key="gate",
        source=origin,
        destination=NavigationNode(0, 0, 2, 1),
        kind="gate",
        cost=1,
        requirements=(requirement(f"var:{VAR:#x}:lt:1", capabilities),),
    )

    assert edge.available is False
    assert edge.unmet
