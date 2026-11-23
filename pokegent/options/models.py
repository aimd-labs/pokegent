"""Decision contract (spec 5.1, 5.2, 6.C, 8.1): Option, DecisionFrame, ChoiceResult, Decider.

Everything here is data plus validation. No emulator, no reader, no I/O.

Invariants enforced by the types (not by convention):

* a :class:`DecisionFrame` is deeply immutable (frozen models, tuples, read-only mappings);
* ``frame.options`` is exactly the chooseable set: LEGAL and CONDITIONAL options, nothing else.
  UNAVAILABLE / UNKNOWN / UNSUPPORTED options live in ``frame.diagnostics`` and are never offered;
* option ids are unique per frame (across both lists);
* a chooseable option names an ``executor_ref``; the model never sees it;
* :func:`to_system_one_payload` is the only serializer for System One and emits only ``state``,
  ``instructions`` and ``criteria`` (``{option_id: concise English description}``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.actions._exec.contracts import ActionContract
from pokegent.errors import ErrorCode
from pokegent.frozen import FrozenDict, FrozenMap, freeze  # noqa: F401  (re-exported)
from pokegent.options.render import (
    DEFAULT_MAX_CHARS,
    move_verdict,
    render_adapter_choice,
    render_facts,
    render_field_criterion,
    render_item_criterion,
    route_objective_slot,
)
from pokegent.state.catalog import RESERVED_SCENE_IDS, SCENE_ID_RE

SCHEMA_VERSION = "1.0"
# Runner-only metadata consumed when criteria are rendered; it never belongs in ``state``.
REPETITION_KEY = "_option_repetition"

OPTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[A-Za-z0-9_.\-]+)*$")
REF_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Stable machine-readable fact used by option builders and external consumers. Human-facing
# wording belongs in rendered descriptions, not in protocol/filter decisions.
OBJECTIVE_RELATION_NOT_SERVING = "does_not_serve_current_objective"


def fingerprint(scene_id: str, **parts: Any) -> str:
    """Canonical ``sha256:`` digest builders use for ``state_fingerprint``.

    ``parts`` are the decision-relevant inputs of the scene (and nothing volatile: no frame
    counter, no wall clock). Two frames of the same scene are interchangeable iff the digests
    are equal, so what is *left out* is what may change under a pending decision.
    """
    payload = {"scene": scene_id, "parts": freeze(parts)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


# -- vocabulary -----------------------------------------------------------------------------


class Legality(str, Enum):
    """Spec 5.2. Only LEGAL and CONDITIONAL are ever offered to a decider."""

    LEGAL = "LEGAL"  # precondition proven
    CONDITIONAL = "CONDITIONAL"  # input legal, outcome uncertain (miss, flee roll)
    UNAVAILABLE = "UNAVAILABLE"  # provably not selectable
    UNKNOWN = "UNKNOWN"  # preconditions unreadable
    UNSUPPORTED = "UNSUPPORTED"  # recognized, deliberately not implemented

    @property
    def chooseable(self) -> bool:
        return self in (Legality.LEGAL, Legality.CONDITIONAL)


class Risk(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- option ---------------------------------------------------------------------------------


class Option(_Frozen):
    """One selectable thing in a scene, instantiated from real RAM/UI data (spec 5.1, 6.C)."""

    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=600)
    legality: Legality
    facts: FrozenMap = Field(default_factory=dict)
    executor_ref: str | None = None
    executor_args: FrozenMap = Field(default_factory=dict)
    risk: Risk = Risk.NORMAL
    postcondition_ref: str | None = None
    #: Spec-6 vocabulary token this option instantiates (``USE_MOVE``); drives the
    #: ``wrong_options`` check of the coverage report. Optional.
    verb: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    # -- spec 6.C: context duty for every generic choice, above all YES/NO -------------------
    semantic_prompt: str | None = None  # the question the game asks (English)
    effect_description: str | None = None  # what choosing this really does (English)
    irreversible: bool = False
    source_task: str | None = None  # internal: task/callback that owns the prompt (never sent)
    expected_postcondition: str | None = (
        None  # internal prose; the machine form is postcondition_ref
    )
    #: Additive adapter contract used by executors and verifiers.
    action_contract: ActionContract | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Option:
        if not OPTION_ID_RE.match(self.id):
            raise ValueError(f"malformed option id {self.id!r}")
        for name in ("executor_ref", "postcondition_ref"):
            ref = getattr(self, name)
            if ref is not None and not REF_RE.match(ref):
                raise ValueError(f"{name} must look like 'namespace.name', got {ref!r}")
        if self.legality.chooseable and self.executor_ref is None:
            raise ValueError(
                f"option {self.id}: a {self.legality.value} option needs an executor_ref"
            )
        if self.irreversible:
            if not (self.semantic_prompt and self.effect_description):
                raise ValueError(
                    f"option {self.id}: an irreversible option must carry semantic_prompt and "
                    "effect_description (spec 6.C)"
                )
            if self.risk is not Risk.HIGH:
                raise ValueError(f"option {self.id}: an irreversible option must have risk 'high'")
        return self

    @property
    def chooseable(self) -> bool:
        return self.legality.chooseable


# -- decision frame -------------------------------------------------------------------------


class DecisionFrame(_Frozen):
    """Everything one decision is based on (spec 5.1). Immutable.

    ``options`` is the chooseable set. ``diagnostics`` lists options that exist but must not be
    offered (UNAVAILABLE/UNKNOWN/UNSUPPORTED), for logs and the coverage report only.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    decision_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(max_length=64)
    snapshot_seq: int = Field(ge=0)
    scene_id: str
    scene_confidence: float = Field(ge=0.0, le=1.0)
    state_fingerprint: str
    goal_id: str | None = None
    #: The objective revision this frame was built under (``None`` when unknown). Captured at
    #: the same build seam as ``goal_id``; it is internal identity and is never included in the
    #: System One payload.
    goal_revision: int | None = Field(default=None, ge=0)
    decision_type: Literal["choice"] = "choice"
    deadline_ms: int = Field(default=1500, gt=0, le=600_000)
    context: FrozenMap = Field(default_factory=dict)
    #: Tactical text for the decider (contract v1.5, :mod:`pokegent.instructions`): set by the
    #: runner on the copy the decider gets, never by builders. ``None`` = the generic one-liner.
    instructions: str | None = Field(default=None, min_length=20, max_length=600)
    options: tuple[Option, ...] = ()
    diagnostics: tuple[Option, ...] = ()
    #: Legal options that a constructor deliberately filtered from the normal situation view.
    #: They are never serialized to a provider payload, but an external controller may request
    #: them explicitly through its ``?all=1`` view.
    all_options: tuple[Option, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> DecisionFrame:
        if not (SCENE_ID_RE.match(self.scene_id) or self.scene_id in RESERVED_SCENE_IDS):
            raise ValueError(f"malformed scene id {self.scene_id!r}")
        if not FINGERPRINT_RE.match(self.state_fingerprint):
            raise ValueError("state_fingerprint must be 'sha256:' + 64 hex digits")
        ids = [o.id for o in self.options] + [o.id for o in self.diagnostics]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"option ids must be unique per frame; duplicated: {dup}")
        bad = [o.id for o in self.options if not o.chooseable]
        if bad:
            raise ValueError(
                f"non-chooseable options in the chooseable set: {bad} "
                "(UNAVAILABLE/UNKNOWN/UNSUPPORTED belong in 'diagnostics')"
            )
        wrong = [o.id for o in self.diagnostics if o.chooseable]
        if wrong:
            raise ValueError(f"chooseable options hidden in 'diagnostics': {wrong}")
        hidden_ids = [o.id for o in self.all_options]
        if len(hidden_ids) != len(set(hidden_ids)):
            raise ValueError("all_options contains duplicate option ids")
        if set(hidden_ids) & {o.id for o in self.options}:
            raise ValueError(
                "all_options duplicates visible option ids: "
                f"{sorted(set(hidden_ids) & {o.id for o in self.options})}"
            )
        if any(not o.chooseable for o in self.all_options):
            raise ValueError("all_options may contain only legal or conditional options")
        return self

    # -- views ---------------------------------------------------------------------------
    def chooseable(self) -> tuple[Option, ...]:
        """LEGAL + CONDITIONAL options, in frame order (this is what a decider may pick from)."""
        return tuple(o for o in self.options if o.legality.chooseable)

    def chooseable_ids(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.chooseable())

    def all_chooseable(self) -> tuple[Option, ...]:
        return (*self.chooseable(), *self.all_options)

    def all_chooseable_ids(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.all_chooseable())

    def get(self, option_id: str) -> Option | None:
        """Any option of the frame, chooseable or diagnostic."""
        for o in self.options:
            if o.id == option_id:
                return o
        for o in self.all_options:
            if o.id == option_id:
                return o
        for o in self.diagnostics:
            if o.id == option_id:
                return o
        return None

    def forced_option(self) -> Option | None:
        """The only chooseable option, if there is exactly one (no decision to make)."""
        chooseable = self.chooseable()
        return chooseable[0] if len(chooseable) == 1 else None

    @property
    def generation(self) -> tuple[int, str]:
        """What "the same frame" means: snapshot sequence plus decision-relevant fingerprint."""
        return (self.snapshot_seq, self.state_fingerprint)

    @classmethod
    def from_options(cls, options: Iterable[Option], **fields: Any) -> DecisionFrame:
        """Split a builder's raw option list by legality into ``options`` / ``diagnostics``."""
        raw = tuple(options)
        return cls(
            options=tuple(o for o in raw if o.legality.chooseable),
            diagnostics=tuple(o for o in raw if not o.legality.chooseable),
            **fields,
        )


# -- System One serialization (spec 5.1, 8.2, 8.3) ---------------------------------------------

_INSTRUCTIONS = {
    "choice": (
        "Current-frame facts and the available option criteria are provided below. "
        "The response value is one option key."
    )
}
#: the one-liner every decision carries when no tactical text applies (also the flat default)
GENERIC_INSTRUCTIONS = _INSTRUCTIONS["choice"]
_HEX_POINTER = re.compile(r"0x[0-9a-fA-F]{6,}")
# Stable navigation keys are executor-facing identifiers, not player-facing prose.
_EDGE_KEY = re.compile(
    r"\b(?:"
    r"warp:\d+(?::\d+){2,}|"
    r"move:\d+(?::\d+){3,}:[NSEW]|"
    r"edge:[NSEW]:\d+:\d+|"
    r"(?:cut|rock_smash|strength|poke_flute|elevator):\d+|"
    r"boulder_plan:[A-Za-z0-9_]+:[A-Za-z0-9_:]+"
    r")\b"
)
# Border-edge keys only: the route may name an edge the frame does not offer as an option.
_ROUTE_EDGE_KEY = re.compile(r"\bedge:[NSEW]:\d+:\d+\b")
_INTERNAL_KEYS = ("executor_ref", "executor_args", "postcondition_ref", "source_task", "pointer")
LeakKind = Literal[
    "fingerprint",
    "decision_id",
    "run_id",
    "executor_ref",
    "postcondition_ref",
    "source_task",
    "executor_arg",
]


class PayloadLeakError(ValueError):
    """The System One payload would expose an executor reference, argument, pointer or id."""

    code = ErrorCode.E_PAYLOAD_LEAK

    def __init__(self, message: str, *, kind: LeakKind | None = None, token: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.token = token

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind or "unknown",
            "token": self.token or "",
            "reason": str(self),
        }


def criterion_text(option: Option, *, repetition: str | None = None) -> str:
    """Concise English description of one option as the model sees it."""
    adapter = render_adapter_choice(
        label=option.label,
        facts=option.facts,
        repetition=repetition,
    )
    if adapter is not None:
        return adapter
    item = render_item_criterion(
        label=option.label,
        facts=option.facts,
        irreversible=option.irreversible,
        repetition=repetition,
    )
    if item is not None:
        return item
    field = render_field_criterion(
        label=option.label,
        facts=option.facts,
        effect_description=option.effect_description,
        irreversible=option.irreversible,
        repetition=repetition,
    )
    if field is not None:
        return field
    text = option.label
    verdict = move_verdict(option.facts) if option.id.startswith("move:") else None
    if option.description:
        if verdict:
            text = f"{option.label}: {verdict.rstrip('. ')}. {option.description}"
            option_description_handled = True
        else:
            option_description_handled = False
        details = (
            option.description
            if option.description.startswith(option.label)
            else (f"{option.label}: {option.description}")
        )
        if not option_description_handled:
            text = details
    elif verdict:
        text = f"{option.label}: {verdict.rstrip('. ')}."
    text = text.rstrip(". ")
    route_slot = route_objective_slot(option.facts)
    if route_slot is not None:
        # Lead with the route relation so length caps can never cut it off.
        rest = text[len(option.label) :].lstrip(":. ") if text.startswith(option.label) else text
        text = f"{option.label}: Objective: {route_slot}" + (f". {rest}" if rest else "")
    if repetition:
        text += f". {repetition.rstrip('. ')}"
    if option.effect_description:
        text += f". Effect: {option.effect_description.rstrip('. ')}"
    consequences = render_facts(option.facts, battle_move=option.id.startswith("move:"))
    if consequences:
        text += f". Consequences: {consequences}"
    if option.irreversible:
        text += ". Irreversible"
    if option.legality is Legality.CONDITIONAL:
        text += ". Outcome uncertain"
    text += "."
    if len(text) > DEFAULT_MAX_CHARS:
        # The route relation leads the text, so the cap only ever trims trailing detail.
        text = text[: DEFAULT_MAX_CHARS - 3].rstrip() + "..."
    return text


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _ordinary_word(value: str) -> bool:
    """Whether an executor token is ordinary player-facing prose."""
    return value.isalpha() and value.islower()


def _internal_tokens(frame: DecisionFrame) -> tuple[tuple[LeakKind, str], ...]:
    tokens: list[tuple[LeakKind, str]] = [
        ("fingerprint", frame.state_fingerprint),
        ("decision_id", frame.decision_id),
    ]
    if frame.run_id:
        tokens.append(("run_id", frame.run_id))
    for option in (*frame.options, *frame.diagnostics):
        for kind, ref in (
            ("executor_ref", option.executor_ref),
            ("postcondition_ref", option.postcondition_ref),
            ("source_task", option.source_task),
        ):
            if ref:
                tokens.append((kind, ref))
        tokens.extend(
            ("executor_arg", arg)
            for arg in _strings(option.executor_args)
            if len(arg) >= 6 and not _ordinary_word(arg)
        )
    return tuple((kind, token) for kind, token in tokens if token)


def sanitize_internal_text(value: Any, frame: DecisionFrame) -> Any:
    """Neutralize known internal tokens in model-facing text, preserving its meaning."""
    if isinstance(value, str):
        for _, token in sorted(
            _internal_tokens(frame), key=lambda item: len(item[1]), reverse=True
        ):
            value = value.replace(token, "[internal reference removed]")
        return value
    if isinstance(value, Mapping):
        return {key: sanitize_internal_text(item, frame) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_internal_text(item, frame) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_internal_text(item, frame) for item in value)
    return value


def sanitize_route_edge_keys(value: Any, option_ids: set[str]) -> Any:
    """Relabel raw ``edge:D:g:n`` keys that are not exposed options in visible route text.

    The planner's route can name a border edge the frame does not offer (#178). The key is not
    an option identity, so it is rendered as prose instead of tripping the leak guard, which
    stays unchanged as the backstop for every other internal key.
    """
    if isinstance(value, str):
        return _ROUTE_EDGE_KEY.sub(
            lambda m: m.group(0) if m.group(0) in option_ids else "the map edge", value
        )
    if isinstance(value, Mapping):
        return {key: sanitize_route_edge_keys(item, option_ids) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_route_edge_keys(item, option_ids) for item in value]
    return value


def assert_payload_clean(payload: Mapping[str, Any], frame: DecisionFrame) -> None:
    """Fail closed if anything internal would reach the model."""
    if set(payload) != {"state", "instructions", "criteria"}:
        raise PayloadLeakError(
            f"payload keys must be state/instructions/criteria: {sorted(payload)}"
        )
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if _HEX_POINTER.search(text):
        token = _HEX_POINTER.search(text).group(0)
        raise PayloadLeakError(
            "payload contains a memory-pointer-like hex literal",
            kind="executor_arg",
            token=token,
        )
    for key in _INTERNAL_KEYS:
        if f'"{key}"' in text:
            kind: LeakKind = {
                "executor_ref": "executor_ref",
                "executor_args": "executor_arg",
                "postcondition_ref": "postcondition_ref",
                "source_task": "source_task",
                "pointer": "executor_arg",
            }[key]
            raise PayloadLeakError(
                f"payload contains the internal key {key!r}", kind=kind, token=key
            )
    option_ids = {option.id for option in frame.options}
    for kind, token in _internal_tokens(frame):
        if kind == "executor_arg" and token in option_ids:
            continue
        if token in text:
            raise PayloadLeakError("payload contains an internal reference", kind=kind, token=token)
    # Criteria keys are option identities by contract and may be stable keys. Their rendered
    # descriptions, and all state values, must remain player-readable.
    visible_values = {"state": payload["state"], "instructions": payload["instructions"]}
    criteria = payload["criteria"]
    if isinstance(criteria, Mapping):
        visible_values["criteria"] = tuple(criteria.values())
    visible_text = json.dumps(visible_values, sort_keys=True, ensure_ascii=False)
    edge_match = next(
        (match for match in _EDGE_KEY.finditer(visible_text) if match.group(0) not in option_ids),
        None,
    )
    if edge_match:
        raise PayloadLeakError(
            "payload contains a navigation edge key",
            kind="executor_arg",
            token=edge_match.group(0),
        )
    # short string arguments leak only if they appear as a whole value
    exact = {
        a
        for o in frame.options
        for a in _strings(o.executor_args)
        if len(a) < 6 and not _ordinary_word(a)
    }
    for value in _strings(payload):
        if value in exact and len(value) >= 3:
            raise PayloadLeakError(
                f"payload exposes an executor argument value {value!r}",
                kind="executor_arg",
                token=value,
            )


def to_system_one_payload(frame: DecisionFrame) -> dict[str, Any]:
    """The only thing a System One provider may be given for a frame.

    ``{"state": ..., "instructions": ..., "criteria": {option_id: English description}}``.
    Executor references and arguments, postcondition refs, pointers, fingerprints and ids are
    never included (and checked: :class:`PayloadLeakError`). Diagnostic options are not offered.
    """
    state: dict[str, Any] = {}
    if isinstance(frame.context.get("story"), str):
        state["story"] = frame.context["story"]
    state["scene"] = frame.scene_id
    if frame.goal_id:
        state["goal"] = frame.goal_id
    repetition = frame.context.get(REPETITION_KEY)
    if not isinstance(repetition, Mapping):
        repetition = {}
    for key, value in frame.context.items():
        if key == REPETITION_KEY:
            continue
        state.setdefault(key, value)
    prompts = []
    for o in frame.chooseable():
        if o.semantic_prompt and o.semantic_prompt not in prompts:
            prompts.append(o.semantic_prompt)
    if prompts:
        state["prompt"] = " | ".join(prompts)
    option_ids = {o.id for o in frame.options}
    payload = {
        "state": sanitize_route_edge_keys(
            json.loads(
                json.dumps(
                    {
                        **state,
                        **(
                            {"objective": sanitize_internal_text(state["objective"], frame)}
                            if "objective" in state
                            else {}
                        ),
                    }
                )
            ),
            option_ids,
        ),
        "instructions": sanitize_route_edge_keys(
            sanitize_internal_text(frame.instructions or _INSTRUCTIONS[frame.decision_type], frame),
            option_ids,
        ),
        "criteria": {
            o.id: sanitize_route_edge_keys(
                criterion_text(o, repetition=repetition.get(o.id)), option_ids
            )
            for o in frame.chooseable()
        },
    }
    assert_payload_clean(payload, frame)
    return payload


# -- choice result and deciders (spec 8.1) -----------------------------------------------------


class ChoiceResult(_Frozen):
    """What a System One provider (or a stub) answers. ``option_id`` is a claim, not a command:
    the runner validates it against the frame before anything is executed."""

    option_id: str
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    model_id: str = ""
    provider_id: str = ""
    latency_ms: float = Field(default=0.0, ge=0.0)
    truncated: bool = False
    raw_response_ref: str = ""
    #: Echo of ``DecisionFrame.decision_id`` when the provider keeps it; a mismatch means the
    #: answer belongs to an older frame (extension to spec 8.1, optional).
    decision_id: str | None = None

    @model_validator(mode="after")
    def _finite(self) -> ChoiceResult:
        if self.confidence is not None and not (
            math.isfinite(self.confidence) and 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be a finite number in [0, 1]")
        if not math.isfinite(self.latency_ms):
            raise ValueError("latency_ms must be finite")
        for key, p in self.probabilities.items():
            if not (math.isfinite(p) and 0 <= p <= 1):
                raise ValueError(f"probability for {key!r} is not a finite number in [0, 1]")
        return self


def check_choice(
    frame: DecisionFrame, result: ChoiceResult, *, allow_all: bool = False
) -> ErrorCode | None:
    """Validate an answer against its frame.

    Provider/model answers see only the ordinary visible choices.  The external controller is
    the sole caller allowed to opt into the constructor's legal, focus-filtered lineage.
    """
    if result.decision_id is not None and result.decision_id != frame.decision_id:
        return ErrorCode.E_STALE_DECISION
    allowed = set(frame.all_chooseable_ids() if allow_all else frame.chooseable_ids())
    if result.option_id not in allowed:
        return ErrorCode.E_CHOICE_NOT_IN_FRAME
    if any(key not in allowed for key in result.probabilities):
        return ErrorCode.E_CHOICE_MALFORMED
    return None


class DeciderError(RuntimeError):
    """The decider could not produce an answer."""

    code = ErrorCode.E_DECIDER_FAILED


class DeciderTimeout(DeciderError):
    code = ErrorCode.E_DECIDER_TIMEOUT


class AgentHandoffTimeout(DeciderTimeout):
    """An explicitly bounded escalated hybrid handoff expired without a choice.

    This is not a provider failure or a shared decider timeout: the caller requested the
    bounded agent wait and no local default is authorized, so the run must pause.
    """


class StaleDecision(DeciderError):
    """Idle emulation invalidated the offered state; request a fresh decision, no input."""

    code = ErrorCode.E_STALE_DECISION


@runtime_checkable
class Decider(Protocol):
    """Picks one option id from a frame. Synchronous; an async provider adapts to this."""

    def decide(self, frame: DecisionFrame) -> ChoiceResult: ...


class ForcedDecider:
    """Answers only when there is nothing to decide (exactly one chooseable option)."""

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        only = frame.forced_option()
        if only is None:
            raise DeciderError(
                f"ForcedDecider needs exactly one chooseable option, "
                f"frame has {len(frame.chooseable())}"
            )
        return ChoiceResult(
            option_id=only.id,
            confidence=1.0,
            model_id="forced",
            provider_id="local",
            decision_id=frame.decision_id,
        )


class FirstLegalDecider:
    """Deterministic TEST STUB: picks the first chooseable option in frame order.

    It exists so the runner, executor and soak tests run without a model. It is NOT a policy and
    must never be wired in production: it ignores the scene, the goal and every fact. Its
    ``provider_id`` is ``"stub"`` so events reveal it.
    """

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        chooseable = frame.chooseable()
        if not chooseable:
            raise DeciderError("FirstLegalDecider: frame has no chooseable option")
        return ChoiceResult(
            option_id=chooseable[0].id,
            model_id="stub:first-legal",
            provider_id="stub",
            decision_id=frame.decision_id,
        )
