"""Game-owned, lazily imported story fallback helpers (#139).

The helpers only name the existing progression loader and identity validator at call time, so
existing module monkeypatches still apply. They add no data, schema, policy, address or
verification claim of their own.
"""

from __future__ import annotations

from typing import Any


def make_story_fallback(path: Any = None, directory: Any = None) -> Any:
    """Load the one external progression artifact through the existing loader."""
    from pokegent.goals.story import load_progression

    return load_progression(path=path, directory=directory)


def verify_story_identity(graph: Any, rom_path: Any) -> None:
    """Verify one loaded story graph through the existing identity validator."""
    from pokegent.goals.story_identity import verify_identity

    verify_identity(graph, rom_path)
