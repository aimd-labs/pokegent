"""Battle scene family (spec 6.D): reader, detectors, option builder, executor macros, verifiers.

See ``the runtime contracts`` for scope, scene coverage and the verified/unverified
symbol list.
Wiring (the integrator calls it from ``pokegent.actions.register_all``)::

    from pokegent.actions.battle.register import register
    register(detectors, builders, executors, verifiers, facts=...)
"""
