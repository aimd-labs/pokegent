"""Synthetic contract tests for the opt-in read-only event SDK page facade.

Only caller-supplied, already-typed ``EventEnvelope`` values and an invented admitted selection
are used. No log, ROM, runtime, server, provider, network or game input is involved.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import FrozenInstanceError, dataclass

import pytest

from pokegent.events import EventEnvelope
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.sdk_events import EventPage, EventRow, EventSdk, EventSdkRejected

ROM = b"synthetic-event-sdk-rom" * 8
RUN = "run-alpha"
OTHER = "run-bravo"


@dataclass
class _Plugin:
    descriptor: GamePluginDescriptor

    def compose(self) -> object:
        return object()


def _selection() -> object:
    descriptor = GamePluginDescriptor(
        plugin_id="firered:synthetic-event",
        game="firered",
        profile_id="synthetic-event",
        contract_version=1,
        rom_sha1=hashlib.sha1(ROM).hexdigest(),
        capabilities=frozenset(),
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(_Plugin(descriptor))
    return registry.select(rom_bytes=ROM, game="firered", profile_id="synthetic-event")


def _env(seq: int, *, run: str = RUN, public: bool = True, **extra: object) -> EventEnvelope:
    values: dict[str, object] = {
        "id": f"{run}:{seq}",
        "run_id": run,
        "seq": seq,
        "observed_at": "2026-11-26T06:00:00Z",
        "type": "action.started",
        "public_visibility": public,
        **extra,
    }
    return EventEnvelope(**values)


def _sdk() -> EventSdk:
    return EventSdk(_selection(), enabled=True)


def _page(events, **kwargs):
    kwargs.setdefault("run_id", RUN)
    return _sdk().read_page(events, **kwargs)


def test_constructor_rejects_bad_input() -> None:
    with pytest.raises(TypeError):
        EventSdk(object())
    with pytest.raises(TypeError):
        EventSdk(_selection(), enabled=1)


def test_disabled_refuses_before_consuming() -> None:
    consumed = 0

    def rows():
        nonlocal consumed
        for seq in range(1, 4):
            consumed += 1
            yield _env(seq)

    with pytest.raises(EventSdkRejected):
        EventSdk(_selection()).read_page(rows(), run_id=RUN)
    assert consumed == 0


def test_exact_run_and_public_filter() -> None:
    page = _page([
        _env(1, run=OTHER),
        _env(2),
        _env(3, public=False),
        _env(4, run=OTHER),
        _env(5),
    ])
    assert isinstance(page, EventPage)
    assert [row.seq for row in page.rows] == [2, 5]
    assert page.next_seq == 5


def test_rows_expose_metadata_only() -> None:
    page = _page([
        _env(
            1,
            scene_id="field.free",
            outcome="applied",
            emulator_frame=42,
            request_id="request-secret",
            provider="provider-secret",
            details={"api_key": "secret-value", "note": "private"},
        )
    ])
    row = page.rows[0]
    assert isinstance(row, EventRow)
    assert dataclasses.fields(EventRow) and {f.name for f in dataclasses.fields(EventRow)} == {
        "id",
        "seq",
        "observed_at",
        "type",
        "scene_id",
        "outcome",
        "emulator_frame",
    }
    assert row.scene_id == "field.free"
    assert row.outcome == "applied"
    assert row.emulator_frame == 42
    assert "secret" not in repr(page)
    assert not hasattr(row, "details")


def test_pagination_traverses_private_tail() -> None:
    events = [_env(1), _env(2, public=False), _env(3, public=False), _env(4)]
    first = _page(events, limit=1)
    second = _page(events, after_seq=first.next_seq, limit=1)
    tail = _page(events, after_seq=second.next_seq)
    assert [row.seq for row in first.rows] == [1]
    assert first.next_seq == 1
    assert [row.seq for row in second.rows] == [4]
    assert second.next_seq == 4
    assert tail.rows == ()
    assert tail.next_seq == 4


def test_limit_never_skips_eligible_rows() -> None:
    events = [_env(1), _env(2), _env(3, public=False), _env(4), _env(5, public=False)]
    first = _page(events, limit=2)
    second = _page(events, after_seq=first.next_seq, limit=2)
    assert [row.seq for row in first.rows] == [1, 2]
    assert first.next_seq == 2
    assert [row.seq for row in second.rows] == [4]
    assert second.next_seq == 5


def test_full_count_without_extra_row_is_complete() -> None:
    page = _page([_env(1), _env(2), _env(3, public=False)], limit=2)
    assert [row.seq for row in page.rows] == [1, 2]
    assert page.next_seq == 3


def test_truncation_cursor_stays_before_unreturned_row() -> None:
    events = [_env(1), _env(2), _env(3), _env(4, public=False)]
    page = _page(events, limit=2)
    assert [row.seq for row in page.rows] == [1, 2]
    assert page.next_seq == 2
    following = _page(events, after_seq=2, limit=2)
    assert [row.seq for row in following.rows] == [3]
    assert following.next_seq == 4


def test_after_seq_and_empty_tail_are_stable() -> None:
    events = [_env(1), _env(2, public=False), _env(3, public=False)]
    page = _page(events, after_seq=2)
    assert page.rows == ()
    assert page.next_seq == 3
    beyond = _page(events, after_seq=9)
    assert beyond.rows == ()
    assert beyond.next_seq == 9


@pytest.mark.parametrize("run_id", ["", "   ", " padded ", "x" * 201, 7, None, True])
def test_bad_run_id_rejected(run_id: object) -> None:
    with pytest.raises(EventSdkRejected):
        _page([_env(1)], run_id=run_id)


@pytest.mark.parametrize("after_seq", [-1, True, 1.0, "1", None])
def test_bad_after_seq_rejected(after_seq: object) -> None:
    with pytest.raises(EventSdkRejected):
        _page([_env(1)], after_seq=after_seq)


@pytest.mark.parametrize("limit", [0, 101, True, False, 1.0, "1", None])
def test_bad_limit_rejected(limit: object) -> None:
    with pytest.raises(EventSdkRejected):
        _page([_env(1)], limit=limit)


def test_non_iterable_and_malformed_rows_rejected() -> None:
    with pytest.raises(EventSdkRejected):
        _page(object())
    with pytest.raises(EventSdkRejected):
        _page([_env(1), {"id": "not-an-envelope"}])
    with pytest.raises(EventSdkRejected):
        _page([_env(1), "not-an-envelope"])


def test_oversize_input_rejected_before_page() -> None:
    consumed = 0

    def rows():
        nonlocal consumed
        for seq in range(1, 10002):
            consumed += 1
            yield _env(seq)

    with pytest.raises(EventSdkRejected):
        _page(rows(), limit=1)
    assert consumed == 10001


def test_duplicate_and_nonmonotonic_sequences_rejected() -> None:
    with pytest.raises(EventSdkRejected):
        _page([_env(1), _env(1)])
    with pytest.raises(EventSdkRejected):
        _page([_env(1), _env(3), _env(2)])
    with pytest.raises(EventSdkRejected):
        _page([_env(2), _env(3, run=OTHER), _env(1)])


def test_other_run_order_is_ignored() -> None:
    page = _page([_env(5, run=OTHER), _env(1), _env(9, run=OTHER), _env(2)])
    assert [row.seq for row in page.rows] == [1, 2]


def test_frozen_output_detached_from_caller() -> None:
    events = [_env(1)]
    page = _page(events)
    events.append(_env(2))
    assert isinstance(page.rows, tuple)
    assert [row.seq for row in page.rows] == [1]
    with pytest.raises(FrozenInstanceError):
        page.rows[0].seq = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        page.next_seq = 2  # type: ignore[misc]
