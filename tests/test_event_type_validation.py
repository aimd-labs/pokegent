from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pokegent.events import EventEnvelope, EventType


def _envelope(event_type: str) -> EventEnvelope:
    return EventEnvelope(
        id="synthetic:1",
        run_id="synthetic",
        seq=1,
        observed_at=datetime.now(timezone.utc).isoformat(),
        type=event_type,
    )


def test_plan_budget_event_uses_dotted_public_name():
    assert EventType.PLAN_BUDGET_EXCEEDED.value == "navigation.plan_budget_exceeded"
    assert _envelope(EventType.PLAN_BUDGET_EXCEEDED.value).type == "navigation.plan_budget_exceeded"


def test_undotted_plan_budget_event_name_is_rejected():
    with pytest.raises(ValidationError):
        _envelope("plan_budget_exceeded")
