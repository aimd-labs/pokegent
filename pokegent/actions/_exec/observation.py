"""The bounded shared adapter observation sidecar."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_BYTES = 64 * 1024
MAX_ENTRIES = 128


class ObservationTooLarge(ValueError):
    """The lossless observation could not fit the shared sidecar bounds."""

    diagnostic = "observation_too_large"


class StuckObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["action_failed", "failed_postcondition", "edges_exhausted", "requirements_unmet"]
    details: Mapping[str, Any] = Field(default_factory=dict)


class AdapterObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["adapter-observation/1"] = Field(
        default="adapter-observation/1", alias="schema"
    )
    seq: int = Field(ge=0)
    snapshot_seq: int = Field(ge=0)
    scene: str = Field(min_length=1, max_length=120)
    story: str | None = Field(default=None, max_length=250)
    objective: Mapping[str, Any] = Field(default_factory=dict)
    loss: Mapping[str, Any] | None = None
    adapter: str | None = None
    model_type: str | None = None
    model: Mapping[str, Any] = Field(default_factory=dict)
    criteria: tuple[Mapping[str, Any], ...] = ()
    shadow: Mapping[str, Any] | None = None
    stuck: StuckObservation | None = None
    payload_leak: Mapping[str, str] | None = None
    diagnostics: tuple[Mapping[str, Any], ...] = ()


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()


def serialize_observation(observation: AdapterObservation) -> bytes:
    """Return canonical JSON, enforcing both shared-surface bounds."""
    data = _json(observation.model_dump(mode="json", by_alias=True))
    while len(data) > MAX_BYTES and observation.criteria:
        diagnostics = (
            *observation.diagnostics,
            {
                "diagnostic": "observation_too_large",
                "entries_truncated": len(observation.criteria),
            },
        )
        observation = observation.model_copy(
            update={"criteria": observation.criteria[:-1], "diagnostics": diagnostics}
        )
        data = _json(observation.model_dump(mode="json", by_alias=True))
    if len(data) > MAX_BYTES:
        raise ObservationTooLarge("adapter observation exceeds 64 KiB")
    return data + b"\n"


def write_observation(path: str | os.PathLike[str], observation: AdapterObservation) -> None:
    """Atomically replace *path* with a validated observation."""
    payload = serialize_observation(observation)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def observation_from_frame(
    frame: Any,
    *,
    seq: int,
    stuck: StuckObservation | None = None,
    verification: Mapping[str, Mapping[str, Any]] | None = None,
    adapter_model: Any | None = None,
    shadow: Any | None = None,
    payload_leak: Mapping[str, str] | None = None,
    loss: Mapping[str, Any] | None = None,
) -> AdapterObservation:
    """Project existing decision data without asking an adapter for new facts."""
    criteria = []
    repetition = frame.context.get("_option_repetition")
    if not isinstance(repetition, Mapping):
        repetition = {}
    original_count = len(frame.options)
    for option in frame.options:
        menu_choice = option.facts.get("menu_choice") is True
        criteria.append(
            {
                "key": option.id,
                "slots": {
                    "objective": option.facts.get("menu_objective")
                    if menu_choice
                    else option.facts.get("serves_goal", "unknown"),
                    "repetition": repetition.get(option.id, "not chosen here yet"),
                    "cost": option.facts.get("menu_cost")
                    if menu_choice
                    else option.facts.get("steps", "unknown"),
                    "prerequisites": option.facts.get("menu_prerequisites")
                    if menu_choice
                    else option.facts.get("needs", []),
                    "novelty": option.facts.get("menu_novelty")
                    if menu_choice
                    else option.facts.get("novelty", "unknown"),
                    "outcome": option.facts.get("menu_outcome")
                    if menu_choice
                    else option.effect_description or option.description,
                    "risk": option.facts.get("menu_risk") if menu_choice else option.risk.value,
                    "reversible": option.facts.get("menu_reversible", True)
                    if menu_choice
                    else not option.irreversible,
                },
                "verify": {
                    "ref": option.postcondition_ref,
                    **dict(
                        verification.get(option.id, {"status": "pending", "reason": None})
                        if verification
                        else {"status": "pending", "reason": None}
                    ),
                },
            }
        )
    diagnostics = [
        {
            "diagnostic": "selection_suppressed",
            "key": option.id,
            "reason": option.facts["selection_reason"],
        }
        for option in frame.diagnostics
        if option.facts.get("selection_reason")
    ]
    diagnostics.extend(
        {
            "diagnostic": "selection_suppressed",
            "key": option.id,
            "reason": option.facts["selection_reason"],
        }
        for option in frame.all_options
        if option.facts.get("selection_reason")
    )
    if len(criteria) > MAX_ENTRIES:
        criteria = criteria[:MAX_ENTRIES]
        diagnostics.append(
            {"diagnostic": "observation_too_large", "entries_truncated": original_count}
        )
    model_data = (
        adapter_model.model_dump(mode="json")
        if adapter_model is not None and hasattr(adapter_model, "model_dump")
        else {}
    )
    surface = model_data.get("surface") if model_data else None
    return AdapterObservation(
        seq=seq,
        snapshot_seq=frame.snapshot_seq,
        scene=frame.scene_id,
        story=frame.context.get("story"),
        objective={"id": frame.goal_id} if frame.goal_id else {},
        loss=dict(loss) if loss else None,
        adapter=surface.split(".", 1)[0] if isinstance(surface, str) else None,
        model_type=type(adapter_model).__name__ if adapter_model is not None else None,
        model=model_data,
        criteria=tuple(criteria),
        shadow=(
            shadow.model_dump(mode="json")
            if shadow is not None and hasattr(shadow, "model_dump")
            else shadow
        ),
        diagnostics=tuple(diagnostics),
        stuck=stuck,
        payload_leak=dict(payload_leak) if payload_leak else None,
    )
