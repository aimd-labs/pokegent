"""The three #198 Controls at the NavigationGraph barrier gate.

Synthetic fixtures (imported from test_barrier_live_evidence): a puzzle
variable alone never proves a barrier open -- the current live tile geometry is
the local proof.  A satisfied variable with a still-blocked live tile stays
closed or unavailable; a satisfied variable with verified-passable current
tiles opens with no stale static requirement attached; and missing proof (no
live geometry, variable unknown) stays unknown, named by its var key.
"""

from test_barrier_live_evidence import BARRIER, _east_edges, _graph, _state

from pokegent.navigation.models import CapabilityState

VARS_100 = CapabilityState(vars=((16548, 100),))


def test_var100_with_a_blocked_live_tile_stays_closed():
    # Control 1: variable at 100 with barrier tiles still blocked is unavailable;
    # the live collision bits, not the variable, close the step.
    graph = _graph("var:16548:100")
    state = _state(blocked=(BARRIER,), player=True)
    assert state.collision(*BARRIER)

    edges = _east_edges(graph, state, live=state, capabilities=VARS_100, include_unmet=True)

    assert edges == ()


def test_var100_with_passable_live_tile_opens_with_no_static_proof():
    # Control 2: variable at 100 with current tiles verified passable is open,
    # and the open walk carries no stale static requirement that could reclose.
    graph = _graph("var:16548:100")
    state = _state(player=True)
    assert state.collision(*BARRIER) == 0

    edges = _east_edges(graph, state, live=state, capabilities=VARS_100, include_unmet=True)

    assert [(edge.kind, edge.available) for edge in edges] == [("walk", True)]
    assert edges[0].requirements == ()


def test_missing_proof_stays_unknown():
    # Control 3: no live geometry and an unknown variable keep the barrier
    # unresolved: an unavailable gate named by its var key, never invented open.
    graph = _graph("var:16548:100")
    edges = _east_edges(
        graph, _state(), live=None, capabilities=CapabilityState(vars_known=False),
        include_unmet=True,
    )

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)
    assert {row.key for edge in edges for row in edge.requirements} == {"var:16548:100"}
