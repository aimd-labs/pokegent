"""Gen-3 battle mechanics: ROM tables, damage and legality facts as pure functions.

Public API (frozen in M2b): ``tables.GameTables``, ``views.{BattlerView, FieldView,
MoveSlotView}``, ``damage.estimate_damage``, ``facts.{move,switch,item,ball}_option_facts``,
``legality.{move_legality, switch_legality, struggle_forced}``. See ``the runtime contracts``.
"""
