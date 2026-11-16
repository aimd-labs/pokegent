"""Frame -> System One question plan (spec 8.2, 8.3; contracts: ``to_system_one_payload``).

The model sees exactly the payload fields: ``state``, ``instructions`` and ``criteria``
(``{option_id: concise English text}``, unchanged). This module adds three things around them:

* a **state budget** (``max_state_tokens``): a deterministic, cheap token estimate and a fixed
  reduction order (below) that shrinks the *context* when it is too big and reports
  ``truncated=True``. Criteria are never cut and never rewritten;
* a **hierarchical plan** for more than ``max_options_per_question`` options: ``choose_category``
  then ``choose_target_in_category``. Categories come from the options' own ``verb`` (else the
  family prefix of the option id), keep frame order, and every option sits in exactly one
  category, so every option stays reachable. Category descriptions only count and name options;
  they never judge them. The whole tree is recorded in the calibration log (open, auditable);
* a stable ``option_set_hash`` over (id, text) of the offered options (spec 16.3);
* an optional **presentation order** of the criteria sent (``option_order``, an experiment switch
  for positional-bias analysis): ``frame`` (default) sends them in frame order, ``shuffle`` in a
  deterministic permutation seeded from (run_id, decision_id). Ids, texts, the option set, the
  hash, legality and the answer mapping (by option id) are the same in both modes.

No I/O, no clock, no global RNG: the same frame yields the same plan (and, under ``shuffle``, the
same order).

State reduction order (``REDUCTION_ORDER``), applied one step at a time until the state fits:

1. ``trim_lists_5``  lists longer than 5 keep 5 items (history-like keys keep the newest, i.e. the
   tail; every other list keeps its head);
2. ``trim_lists_2``  the same down to 2 items;
3. ``drop_key``      optional top-level keys are dropped one at a time: history-like keys first
   (alphabetical), then the others by decreasing estimated size (ties alphabetical);
4. ``shorten_text``  the protected ``prompt`` and ``goal`` strings are cut at word boundaries as a
   last resort. ``scene``, ``selected_category`` are never touched.

If the state still does not fit, ``over_budget`` is reported (the server may then truncate; that
is reported separately from the response).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pokegent.options.models import (
    GENERIC_INSTRUCTIONS,
    DecisionFrame,
    Option,
    assert_payload_clean,
    to_system_one_payload,
)

PROMPT_SCHEMA_VERSION = "so-choice/1"
#: ``system_one.option_order``: the order the criteria are sent in (presentation only)
OPTION_ORDERS = ("frame", "shuffle")
REDUCTION_ORDER = ("trim_lists_5", "trim_lists_2", "drop_key", "shorten_text")
PROTECTED_KEYS = ("scene", "goal", "prompt", "selected_category")
_HISTORY_HINTS = ("recent", "history", "log", "failed", "previous", "past", "notes", "memory")

Q_FLAT = "action"
Q_CATEGORY = "choose_category"
Q_TARGET = "choose_target_in_category"

_PIECE = re.compile(r"[^\W\d_]+|\d+|[^\s\w]|_")


# -- token estimate -----------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Deterministic, cheap upper-ish estimate: one token per word/number/punctuation piece plus
    one per further 6 characters of a long piece (BPE splits long words). Not a tokenizer; the
    real count comes back as ``usage.input_tokens`` and is logged next to this estimate."""
    total = 0
    for m in _PIECE.finditer(text):
        total += 1 + (len(m.group()) - 1) // 6
    return total


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def estimate_state_tokens(state: Mapping[str, Any]) -> int:
    return estimate_tokens(canonical_json(state))


# -- presentation order -------------------------------------------------------------------------


def ordered_keys(
    keys: Sequence[str], mode: str, *, run_id: str = "", decision_id: str = "", salt: str = ""
) -> list[str]:
    """``keys`` in the order they are sent: ``frame`` keeps them, ``shuffle`` sorts them by a
    sha256 of (run_id, decision_id, salt, key): a pure, repeatable permutation (no RNG, no clock,
    no state) in which every key is equally likely to come first. ``salt`` separates the stages of
    a hierarchical plan."""
    if mode == "frame":
        return list(keys)
    if mode != "shuffle":
        raise ValueError(f"option_order must be one of {OPTION_ORDERS}, got {mode!r}")

    def rank(key: str) -> tuple[str, str]:
        seed = "\x1f".join((run_id, decision_id, salt, key))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest(), key

    return sorted(keys, key=rank)


# -- limits and results -------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionLimits:
    max_state_tokens: int = 1200
    max_options_per_question: int = 32
    sample_labels: int = 6  # option labels named in a category description
    label_chars: int = 40

    def __post_init__(self) -> None:
        if self.max_state_tokens < 16:
            raise ValueError("max_state_tokens must be at least 16")
        if self.max_options_per_question < 2:
            raise ValueError("max_options_per_question must be at least 2")


@dataclass(frozen=True)
class StateReduction:
    state: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    tokens_before: int
    tokens_after: int
    over_budget: bool

    @property
    def truncated(self) -> bool:
        return bool(self.steps)


@dataclass(frozen=True)
class Node:
    """One question of the plan. ``children`` empty = leaf: the criteria keys are option ids."""

    key: str  # category key that leads here ("" for the root)
    criteria: Mapping[str, str]
    children: Mapping[str, Node] = field(default_factory=dict)

    @property
    def leaf(self) -> bool:
        return not self.children

    def option_ids(self) -> list[str]:
        if self.leaf:
            return list(self.criteria)
        return [oid for child in self.children.values() for oid in child.option_ids()]

    def describe(self) -> dict[str, Any]:
        """JSON-able audit view (calibration log): who is reachable under which key."""
        if self.leaf:
            return {"options": list(self.criteria)}
        return {k: c.describe() for k, c in self.children.items()}


@dataclass(frozen=True)
class DecisionPlan:
    """Everything needed to ask one frame: state (reduced once), instructions, the question tree."""

    decision_id: str
    state: Mapping[str, Any]
    instructions: str
    root: Node
    criteria: Mapping[str, str]  # every offered option, unchanged from the payload
    reduction: StateReduction
    option_set_hash: str
    criteria_tokens: int
    option_count: int
    limits: QuestionLimits
    #: ``frame`` | ``shuffle``: the order the criteria of every question are sent in
    option_order: str = "frame"
    #: (run_id, decision_id): the seed of the ``shuffle`` permutation
    order_seed: tuple[str, str] = ("", "")

    @property
    def hierarchical(self) -> bool:
        return not self.root.leaf

    @property
    def truncated(self) -> bool:
        return self.reduction.truncated

    def stage_question(self, node: Node, depth: int) -> dict[str, Any]:
        """The wire question for ``node`` (``name`` is the key under ``questions``)."""
        if not self.hierarchical:
            name, instructions = Q_FLAT, self.instructions
        elif not node.leaf:
            name, instructions = Q_CATEGORY, _CATEGORY_INSTRUCTIONS
        else:
            name, instructions = Q_TARGET, _TARGET_INSTRUCTIONS
            if self.instructions and self.instructions != GENERIC_INSTRUCTIONS:
                instructions = f"{self.instructions} {_TARGET_INSTRUCTIONS}"  # tactical text first
        # presentation only: the same ids and texts, possibly in another order (a dict keeps it)
        keys = ordered_keys(
            list(node.criteria),
            self.option_order,
            run_id=self.order_seed[0],
            decision_id=self.order_seed[1],
            salt=f"{depth}:{node.key}",
        )
        return {
            "name": name,
            "type": "choice",
            "instructions": instructions,
            "criteria": {k: node.criteria[k] for k in keys},
        }

    def stage_state(self, path: Sequence[str]) -> dict[str, Any]:
        state = dict(self.state)
        if path:
            state["selected_category"] = path[-1]
        return state

    def audit(self) -> dict[str, Any]:
        """Deterministic summary for the calibration log."""
        return {
            "hierarchical": self.hierarchical,
            "option_count": self.option_count,
            "option_set_hash": self.option_set_hash,
            "state_est_before": self.reduction.tokens_before,
            "state_est_after": self.reduction.tokens_after,
            "state_over_budget": self.reduction.over_budget,
            "reduction": list(self.reduction.steps),
            "criteria_est": self.criteria_tokens,
            "tree": self.root.describe() if self.hierarchical else None,
            "option_order": self.option_order,
        }


_CATEGORY_INSTRUCTIONS = (
    "Current-frame facts and category criteria are provided below. "
    "Each category key identifies available options."
)
_TARGET_INSTRUCTIONS = (
    "Current-frame facts and option criteria for the selected category are provided below. "
    "Each option key identifies an available response."
)


# -- state reduction ----------------------------------------------------------------------------


def _history_like(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in _HISTORY_HINTS)


def _trim_lists(value: Any, keep: int, key: str, paths: list[str], at: str) -> Any:
    if isinstance(value, list):
        out = value
        if len(value) > keep:
            out = value[-keep:] if _history_like(key) else value[:keep]
            paths.append(at)
        return [_trim_lists(v, keep, key, paths, f"{at}[]") for v in out]
    if isinstance(value, dict):
        return {k: _trim_lists(v, keep, k, paths, f"{at}.{k}") for k, v in value.items()}
    return value


def _cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 3, 1)]
    if " " in cut[20:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


def reduce_state(state: Mapping[str, Any], max_tokens: int) -> StateReduction:
    """Fit ``state`` into ``max_tokens`` (estimated) by the documented order. Pure."""
    current: dict[str, Any] = json.loads(canonical_json(state))  # plain, mutable, key-sorted copy
    before = estimate_state_tokens(current)
    steps: list[dict[str, Any]] = []

    def fits() -> bool:
        return estimate_state_tokens(current) <= max_tokens

    if before > max_tokens:
        for keep, name in ((5, "trim_lists_5"), (2, "trim_lists_2")):
            paths: list[str] = []
            trimmed = {
                k: (v if k in PROTECTED_KEYS else _trim_lists(v, keep, k, paths, k))
                for k, v in current.items()
            }
            if paths:
                current = trimmed
                steps.append({"step": name, "paths": sorted(set(paths))})
            if fits():
                break
        if not fits():
            optional = [k for k in current if k not in PROTECTED_KEYS]
            hist = sorted(k for k in optional if _history_like(k))
            rest = sorted(
                (k for k in optional if not _history_like(k)),
                key=lambda k: (-estimate_tokens(canonical_json(current[k])), k),
            )
            for key in (*hist, *rest):
                if fits():
                    break
                del current[key]
                steps.append({"step": "drop_key", "key": key})
        if not fits():
            for key in ("prompt", "goal"):
                value = current.get(key)
                while isinstance(value, str) and not fits() and len(value) > 40:
                    value = _cut_words(value, int(len(value) * 0.7))
                    current[key] = value
                    if not steps or steps[-1] != {"step": "shorten_text", "key": key}:
                        steps.append({"step": "shorten_text", "key": key})
                if fits():
                    break
    after = estimate_state_tokens(current)
    return StateReduction(
        state=current,
        steps=tuple(steps),
        tokens_before=before,
        tokens_after=after,
        over_budget=after > max_tokens,
    )


# -- hierarchy ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    option_id: str
    text: str
    label: str
    group: str


def _group_key(option: Option) -> str:
    if option.verb:
        return option.verb.lower()
    return option.id.split(":", 1)[0]


def _describe_group(group: str, items: Sequence[_Item], limits: QuestionLimits) -> str:
    n = len(items)
    names = [_cut_words(i.label, limits.label_chars) for i in items[: limits.sample_labels]]
    more = f", and {n - len(names)} more" if n > len(names) else ""
    what = group.replace("_", " ")
    return f"{what}: {n} option{'s' if n != 1 else ''}, such as {'; '.join(names)}{more}."


def _chunks(seq: Sequence[Any], max_n: int) -> list[Sequence[Any]]:
    parts = math.ceil(len(seq) / max_n)
    size = math.ceil(len(seq) / parts)  # balanced, each <= max_n
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _leaf(items: Sequence[_Item], key: str) -> Node:
    return Node(key=key, criteria={i.option_id: i.text for i in items})


def _pack(entries: list[tuple[str, str, Node]], max_n: int, key: str) -> Node:
    """Category node over ``entries``; pages of categories when there are more than ``max_n``."""
    if len(entries) <= max_n:
        return Node(
            key=key,
            criteria={k: d for k, d, _ in entries},
            children={k: n for k, _, n in entries},
        )
    pages: list[tuple[str, str, Node]] = []
    for idx, chunk in enumerate(_chunks(entries, max_n), start=1):
        page_key = f"categories_{idx}"
        names = "; ".join(k.replace("_", " ") for k, _, _ in chunk[:8])
        desc = f"{len(chunk)} categories: {names}{'; ...' if len(chunk) > 8 else ''}."
        pages.append((page_key, desc, _pack(list(chunk), max_n, page_key)))
    return _pack(pages, max_n, key)


def _build_tree(items: Sequence[_Item], limits: QuestionLimits) -> Node:
    """Flat when it fits; else one category level (large categories split into ordered parts,
    category pages only if there are still more than ``max_options_per_question`` entries)."""
    max_n = limits.max_options_per_question
    if len(items) <= max_n:
        return _leaf(items, "")
    groups: dict[str, list[_Item]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it)
    entries: list[tuple[str, str, Node]] = []
    for gkey, gitems in groups.items():
        if len(gitems) <= max_n:
            entries.append((gkey, _describe_group(gkey, gitems, limits), _leaf(gitems, gkey)))
            continue
        for idx, part in enumerate(_chunks(gitems, max_n), start=1):  # frame order, contiguous
            pkey = f"{gkey}_part_{idx}"
            entries.append(
                (pkey, _describe_group(f"{gkey} (part {idx})", part, limits), _leaf(part, pkey))
            )
    return _pack(entries, max_n, "")


# -- entry point --------------------------------------------------------------------------------


def option_set_hash(criteria: Mapping[str, str]) -> str:
    digest = hashlib.sha256(canonical_json(sorted(criteria.items())).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def plan_decision(
    frame: DecisionFrame, limits: QuestionLimits | None = None, *, option_order: str = "frame"
) -> DecisionPlan:
    """Build the question plan for ``frame``. Raises ``PayloadLeakError`` (E_PAYLOAD_LEAK) if the
    frame would expose internals: the guard of ``to_system_one_payload`` runs first.

    ``option_order`` ("frame" | "shuffle") only changes the order the criteria are SENT in, see
    :func:`ordered_keys`; the tree, the texts and ``option_set_hash`` do not depend on it."""
    if option_order not in OPTION_ORDERS:
        raise ValueError(f"option_order must be one of {OPTION_ORDERS}, got {option_order!r}")
    limits = limits or QuestionLimits()
    payload = to_system_one_payload(frame)
    criteria: dict[str, str] = payload["criteria"]
    if not criteria:
        raise ValueError("frame has no chooseable option")
    reduction = reduce_state(payload["state"], limits.max_state_tokens)
    by_id = {o.id: o for o in frame.chooseable()}
    items = [
        _Item(oid, text, by_id[oid].label, _group_key(by_id[oid])) for oid, text in criteria.items()
    ]
    root = _build_tree(items, limits)
    plan = DecisionPlan(
        decision_id=frame.decision_id,
        state=reduction.state,
        instructions=payload["instructions"],
        root=root,
        criteria=dict(criteria),
        reduction=reduction,
        option_set_hash=option_set_hash(criteria),
        criteria_tokens=estimate_tokens(canonical_json(criteria)),
        option_count=len(criteria),
        limits=limits,
        option_order=option_order,
        order_seed=(frame.run_id, frame.decision_id),
    )
    # defense in depth: every stage payload passes the same leak guard as the flat one
    _check_stages(frame, plan, root, [])
    reachable = root.option_ids()
    if sorted(reachable) != sorted(criteria):  # a bug here would hide a legal option
        raise RuntimeError("question hierarchy lost an option; refusing to ask")
    return plan


def _check_stages(frame: DecisionFrame, plan: DecisionPlan, node: Node, path: list[str]) -> None:
    q = plan.stage_question(node, len(path))
    assert_payload_clean(
        {
            "state": plan.stage_state(path),
            "instructions": q["instructions"],
            "criteria": q["criteria"],
        },
        frame,
    )
    for key, child in node.children.items():
        _check_stages(frame, plan, child, [*path, key])
