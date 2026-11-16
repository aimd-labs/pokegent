"""Versioned FireRed/Gen-3 battle rules.

The ruleset owns game arithmetic and ROM opcode tables.  Battle builders, legality verdicts,
facts, and renderers remain in the core and consume this provider.
"""

from . import catch, damage, effects


class FireRedRuleset:
    """The FireRed v1 Gen-3 rules provider exposed by :class:`FireRedAdapter`."""

    version = "firered-gen3-v1"
    damage = damage
    catch = catch
    effects = effects


__all__ = ["FireRedRuleset", "catch", "damage", "effects"]
