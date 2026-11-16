"""Stable scene and registry references for START and field-party menus."""

START = "menu.start"
PARTY_LIST = "party.list"
PARTY_ACTION = "party.action"
PARTY_REORDER = "party.reorder"
PARTY_HELD_ITEM = "party.held_item"

CLAIMED_SCENES = frozenset({START, PARTY_LIST, PARTY_ACTION, PARTY_REORDER, PARTY_HELD_ITEM})

REF_SELECT = "start_party.select"
POST_EXPECTED = "start_party.expected"
