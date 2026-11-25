"""Catalog coverage dispositions stay reporting metadata and point at real executors.

The report folds an already-implemented scene onto the executor that owns it.  These tests
build the real ownership report (no emulator or private ROM data) so a wrong disposition
cannot quietly send an implemented mechanic back to the gap list.
"""

from __future__ import annotations

from pokegent.actions import Registries, register_all
from pokegent.actions.coverage import ALL_DISPOSITIONS, FOLDED, NOT_NEEDED_FOR_E4, REAL_GAPS
from pokegent.state.catalog import load_scene_catalog


def _report():
    catalog = load_scene_catalog()
    bundle = register_all(Registries.new(catalog))
    return bundle.registries, bundle.ownership(catalog)


def test_disposition_maps_do_not_share_keys():
    assert not set(FOLDED) & set(NOT_NEEDED_FOR_E4)
    assert not set(FOLDED) & set(REAL_GAPS)
    assert not set(NOT_NEEDED_FOR_E4) & set(REAL_GAPS)
    assert len(ALL_DISPOSITIONS) == len(FOLDED) + len(NOT_NEEDED_FOR_E4) + len(REAL_GAPS)


def test_fishing_is_folded_to_the_registered_rod_executor():
    registries, report = _report()
    disposition = ALL_DISPOSITIONS["field.fishing"]
    assert disposition.kind == "folded"
    assert disposition.ref == "ExecutorSpec:field.fish"
    assert disposition.runtime_scene == "field.free"

    row = next(r for r in report.rows if r.scene_id == "field.fishing")
    assert row.status == "folded"
    assert "ExecutorSpec:field.fish" in row.note
    assert "field.fishing" not in {r.scene_id for r in report.gaps()}
    assert registries.executors.get("field.fish") is not None


def test_unproven_field_hazards_remain_real_gaps():
    _, report = _report()
    gaps = {r.scene_id for r in report.gaps()}
    for scene_id in (
        "field.hidden_item",
        "field.ice_slide",
        "field.sweet_scent",
        "field.unknown_obstacle",
    ):
        assert ALL_DISPOSITIONS[scene_id].kind == "REAL_GAP"
        assert scene_id in gaps
