"""ROM/RAM navigation domain for FireRed.

The adapter owns movement facts and route selection.  Scene families keep ownership of input
execution: a :class:`NavigationEdge` names the existing semantic executor target, never buttons.
"""

from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import (
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.navigation.planner import NavigationPlanner
from pokegent.navigation.regions import RegionGraph, RegionRoute, load_regions
from pokegent.navigation.routing import MapGraph
from pokegent.navigation.serving import serves_goal

__all__ = [
    "CapabilityState",
    "NavigationEdge",
    "NavigationAdapter",
    "NavigationGraph",
    "NavigationNode",
    "NavigationPlan",
    "NavigationPlanner",
    "NavigationRequirement",
    "NavigationTarget",
    "MapGraph",
    "RegionGraph",
    "RegionRoute",
    "load_regions",
    "serves_goal",
]
