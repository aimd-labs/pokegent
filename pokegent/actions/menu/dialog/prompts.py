"""What is the game asking? Prompt classification and the answer policy (spec 6.C, 6.K.4).

There is NO generic YES rule (spec 6.C). The question text decides what YES and NO mean:

* ``HEAL``          a Pokemon Center offer: YES heals the party. Reversible, harmless.
* ``FIELD_MOVE``    "Would you like to use STRENGTH/CUT/SURF ...?": YES uses the field move.
* ``DESTRUCTIVE``   release / toss / discard / overwrite / forget / delete ...: YES is irreversible.
  YES is never offered without an explicit authorization (hard invariant 7, CONTEXT.md); NO stays
  available because declining is the safe answer.
* ``UNKNOWN``       text readable, no known meaning.
* ``UNREADABLE``    no proven-current text (see ``text.py``).

For UNKNOWN and UNREADABLE the default policy is ``escalate``: the options exist (the frame says
what the game shows) but they are UNKNOWN, i.e. diagnostics, not offered; the runner then finds no
chooseable option and escalates. ``unknown_prompts="offer"`` (an owner/agent decision, e.g. in a
test or with a plan that knows the prompt) offers them as CONDITIONAL with the uncertainty spelled
out. The classifier only reads text; it never creates an input.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pokegent.actions.menu.dialog.text import DialogText
from pokegent.local_data import read_data


class PromptKind(str, Enum):
    HEAL = "heal"
    FIELD_MOVE = "field_move"
    POKE_FLUTE = "poke_flute"
    FOSSIL_CHOICE = "fossil_choice"
    SECRET_SWITCH = "secret_switch"
    QUIZ = "quiz"
    DESTRUCTIVE = "destructive"
    ITEM_RECEIVE = "item_receive"
    UNKNOWN = "unknown"
    UNREADABLE = "unreadable"
    PC_STORAGE_ERROR = "pc_storage_error"


def normalize(text: str) -> str:
    """Lower-case ASCII for pattern matching: ``POKeMON`` -> ``pokemon``, typographic quotes."""
    text = text.replace("’", "'").replace("‘", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class PromptRulePolicy:
    """Facts and a safe default attached to one ROM prompt rule.

    ``default_yes`` is only a fallback.  ``PromptPolicy.serves_goal`` may replace it for the
    current objective; the selected value is emitted as a fact so System One sees the reason.
    """

    default_yes: bool | None = None
    field_move: str | None = None
    irreversible: bool = False
    wrong_answer_cost: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptRule:
    kind: str
    patterns: tuple[re.Pattern[str], ...]
    policy: PromptRulePolicy

    def matches(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.patterns)


_RULES: list[PromptRule] = []


def register_rule(
    kind: str | PromptKind,
    patterns: Sequence[str | re.Pattern[str]],
    policy: PromptRulePolicy | Mapping[str, object] | None = None,
) -> PromptRule:
    """Register one ordered, ROM-text-backed prompt rule and return it.

    Patterns are searched against :func:`normalize` output.  Registration is deliberately tiny so
    the special family can add prompt sites without changing ``dialog.yes_no`` or its executor.
    """
    if not patterns:
        raise ValueError("a prompt rule needs at least one pattern")
    value = (
        policy if isinstance(policy, PromptRulePolicy) else PromptRulePolicy(**dict(policy or {}))
    )
    compiled = tuple(
        pattern if hasattr(pattern, "search") else re.compile(normalize(pattern), re.IGNORECASE)
        for pattern in patterns
    )
    rule = PromptRule(str(kind.value if isinstance(kind, PromptKind) else kind), compiled, value)
    _RULES.append(rule)
    return rule


def prompt_rules() -> tuple[PromptRule, ...]:
    """The immutable view used by tests and diagnostics."""
    return tuple(_RULES)


def _register_builtin_rules() -> None:
    # Generic action recognition. Game-specific quiz answers come from local files only.
    for move in (
        "cut",
        "rock smash",
        "strength",
        "surf",
        "flash",
        "fly",
        "waterfall",
        "dive",
        "sweet scent",
        "teleport",
        "dig",
    ):
        register_rule(
            "field_move",
            (rf"would you like to (?:use )?{move}(?: it)?\?",),
            PromptRulePolicy(default_yes=True, field_move=move),
        )
    register_rule(
        PromptKind.POKE_FLUTE,
        (r"want to use the poke fl[u]?te\?",),
        PromptRulePolicy(default_yes=True),
    )
    register_rule(
        PromptKind.FOSSIL_CHOICE,
        (r"(?:do you want|you want) (?:the )?(?:dome|helix) fossil\?",),
        PromptRulePolicy(irreversible=True),
    )
    register_rule(
        PromptKind.SECRET_SWITCH,
        (r"a secret switch!.*press it\?",),
        PromptRulePolicy(default_yes=True),
    )


def _register_local_rules() -> None:
    quiz = read_data("cinnabar_quiz.yaml", optional=True)
    data = read_data("prompt_rules.yaml", optional=True)
    if data is not None:
        for row in data["rules"]:
            policy = {key: row[key] for key in (
                "default_yes", "field_move", "irreversible", "wrong_answer_cost", "metadata"
            ) if key in row}
            if row["kind"] == "quiz" and quiz is not None:
                patterns = {normalize(p.replace("-type", "")) for p in row["patterns"]}
                for question in quiz["questions"]:
                    if normalize(question["prompt"]) in patterns:
                        flag = question["door_flag"]
                        policy["metadata"] = {
                            **policy.get("metadata", {}), "door": question["number"],
                            "door_flag": int(flag, 0) if isinstance(flag, str) else flag,
                        }
            register_rule(row["kind"], tuple(re.escape(p) for p in row["patterns"]), policy)
    if quiz is not None:
        for row in quiz["questions"]:
            flag = row["door_flag"]
            register_rule(
                PromptKind.QUIZ, (re.escape(normalize(row["prompt"])),),
                PromptRulePolicy(
                    default_yes=row["answer_yes"], wrong_answer_cost="trainer battle",
                    metadata={"door": row["number"], "door_flag": (
                        int(flag, 0) if isinstance(flag, str) else flag
                    )},
                ),
            )


_register_builtin_rules()
_register_local_rules()


# Order matters: destructive wins over everything (fail closed).
_DESTRUCTIVE = re.compile(
    r"\b(release\w*|toss\w*|throw(?:n|ing)? away|discard\w*|overwrit\w*|delet\w*|eras\w*|"
    r"forget\w*|abandon\w*|give up|reset\w*)\b"
)
_HEAL = re.compile(r"\bheal (?:your|the|all)\b.*\bpokemon\b|\brestore your pokemon\b")
_FIELD_MOVE = re.compile(
    r"\bwould you like to (?:use )?(cut|surf|strength|flash|rock smash|waterfall|dive|"
    r"sweet scent|teleport|dig|fly)\b"
)
_ITEM = re.compile(
    r"\b(?:obtained|received|found one|found an?|put (?:the |\w+ )?.{0,40} in the bag|"
    r"put away)\b"
)
_BAG_FULL = re.compile(r"\b(?:bag is full|no (?:more )?room|bag.{0,20}full)\b")
_WHITEOUT = re.compile(
    r"\b(?:whited out|out of usable pokemon|scurried to a pokemon center|"
    r"is out of usable)\b"
)
_PC_STORAGE_ERROR = re.compile(
    r"\b(?:party|team)\b.{0,30}\bfull\b|\bno room in (?:(?:the|your) )?(?:party|team)\b|"
    r"\b(?:last|only)\b.{0,20}\b(?:pokemon|pok[eé]mon)\b|"
    r"\bbox\b.{0,20}\bfull\b"
)


@dataclass(frozen=True)
class PromptInfo:
    kind: PromptKind
    reason: str  # why this kind (the pattern or the missing proof); internal
    question: str | None  # the text the options quote (last page), None if unreadable
    full_text: str | None
    digest: str | None  # identity of the text, part of the frame fingerprint
    field_move: str | None = None  # "strength", "cut", ... for FIELD_MOVE
    bag_full: bool = False
    whiteout: bool = False
    rule: PromptRule | None = None
    rule_kind: str | None = None
    default_yes: bool | None = None
    irreversible: bool = False
    wrong_answer_cost: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass
class FieldMoveTrigger:
    """The field option that caused the current field-move question."""

    goal_id: str | None = None
    move: str | None = None
    serves_goal: bool | None = None
    next_route_step: bool = False
    expected_destination: tuple[int, int] | None = None
    on_decline: Callable[[], None] | None = None

    def clear(self) -> None:
        self.goal_id = None
        self.move = None
        self.serves_goal = None
        self.next_route_step = False
        self.expected_destination = None
        self.on_decline = None


def classify_prompt(text: DialogText | None) -> PromptInfo:
    if text is None or not text.pages:
        return PromptInfo(PromptKind.UNREADABLE, "no proven-current text", None, None, None)
    norm_last = normalize(text.last_page)
    norm_all = normalize(text.text)
    base = {
        "question": text.last_page,
        "full_text": text.text,
        "digest": text.digest,
        "bag_full": bool(_BAG_FULL.search(norm_all)),
        "whiteout": bool(_WHITEOUT.search(norm_all)),
    }
    m = _DESTRUCTIVE.search(norm_last) or _DESTRUCTIVE.search(norm_all)
    if m:
        return PromptInfo(PromptKind.DESTRUCTIVE, f"destructive keyword {m.group(1)!r}", **base)
    if _HEAL.search(norm_all):
        return PromptInfo(PromptKind.HEAL, "heal offer", **base)
    if _PC_STORAGE_ERROR.search(norm_all):
        return PromptInfo(
            PromptKind.PC_STORAGE_ERROR,
            "PC storage capacity/last-member error",
            **base,
        )
    rule = next((candidate for candidate in _RULES if candidate.matches(norm_all)), None)
    if rule is not None:
        try:
            kind = PromptKind(rule.kind)
        except ValueError:
            kind = PromptKind.UNKNOWN
        return PromptInfo(
            kind,
            f"registered prompt rule {rule.kind}",
            rule=rule,
            rule_kind=rule.kind,
            default_yes=rule.policy.default_yes,
            field_move=rule.policy.field_move,
            irreversible=rule.policy.irreversible,
            wrong_answer_cost=rule.policy.wrong_answer_cost,
            metadata=rule.policy.metadata,
            **base,
        )
    m = _FIELD_MOVE.search(norm_all)
    if m:
        return PromptInfo(PromptKind.FIELD_MOVE, "field move offer", field_move=m.group(1), **base)
    if _ITEM.search(norm_all):
        return PromptInfo(PromptKind.ITEM_RECEIVE, "item received", **base)
    return PromptInfo(PromptKind.UNKNOWN, "no known pattern", **base)


def prompt_consequences(
    info: PromptInfo, *, tables: Any = None, item_offer: bool = False
) -> tuple[str, str] | None:
    """Return player-readable YES/NO consequences when the prompt text proves them."""
    question = normalize(info.question or "")
    if "continue box operation" in question or "continue box" in question:
        return (
            "continues the current Pokémon box operation",
            "leaves Pokémon storage and returns to the previous PC phase",
        )
    if "nickname" in question:
        return "opens the naming screen", "keeps the default name"
    starter_question = "starter" in question or bool(
        re.search(r"\b(?:grass|fire|water)\s+pokemon\b", question)
    )
    if starter_question:
        names = re.findall(r"\b[A-Z][A-Z0-9'-]{2,}\b", info.question or "")
        species_match = re.search(r"\bPOKEMON\s+([A-Z][A-Z0-9'-]{2,})\b", info.question or "")
        chosen = (
            species_match.group(1)
            if species_match is not None
            else next(
                (name for name in reversed(names) if name not in {"YES", "NO"}),
                "the selected Pokémon",
            )
        )
        types: tuple[str, ...] = ()
        if tables is not None:
            try:
                species = next(
                    row for row in tables.all_species if normalize(row.name) == normalize(chosen)
                )
                types = tuple(tables.type_name(type_id).lower() for type_id in species.types)
            except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
                types = ()
        type_text = f" ({'/'.join(dict.fromkeys(types))})" if types else ""
        return (
            f"confirms {chosen}{type_text} as your starter; cannot be undone",
            "returns to the table to choose again",
        )
    item_name_in_prompt = False
    if tables is not None:
        item_names = {
            normalize(item.name)
            for item in getattr(tables, "items", ())
            if getattr(item, "name", None)
        }
        item_name_in_prompt = any(
            re.search(rf"\b{re.escape(item_name)}\b", question) is not None
            for item_name in item_names
        )
    item_context = item_offer or info.kind is PromptKind.ITEM_RECEIVE or item_name_in_prompt
    if item_context and re.search(r"\b(?:take|accept|receive|keep)\b", question):
        return "adds the offered item to your bag", "leaves the offered item behind"
    if item_context and re.search(r"\bbuy\b", question):
        return "purchases the offered item", "does not purchase the offered item"
    if item_context and re.search(r"\buse\b", question):
        return "uses the offered item", "leaves the game state unchanged"
    return None


# -- policy ----------------------------------------------------------------------------------

Authorizer = Callable[[PromptInfo, "str | None"], "str | None"]
ServesGoal = Callable[[PromptInfo, "str | None"], bool | None]
#: ``labels(menu_id, entry_count) -> labels or None``. The text of a menu entry is not part of the
#: game state this branch can read, so by default no label is known (see builder.py).
LabelProvider = Callable[[int, int], "Sequence[str] | None"]
MenuEntryProvider = Callable[[object, int, int], "Sequence[tuple[str, str]] | None"]


@dataclass(frozen=True)
class PromptPolicy:
    """How prompts of each kind are answered.

    ``authorize(info, goal_id)`` returns an explicit, auditable reason to allow YES on a
    DESTRUCTIVE prompt, or ``None`` (the default: never). ``unknown_prompts`` is ``escalate``
    (default) or ``offer``.
    """

    unknown_prompts: Literal["escalate", "offer"] = "escalate"
    authorize: Authorizer | None = None
    labels: LabelProvider | None = None
    menu_entries: MenuEntryProvider | None = None
    serves_goal: ServesGoal | None = None
    default_answers: Mapping[str, bool] = field(default_factory=dict)
    tables: Any = field(default=None, repr=False, compare=False)
    field_move_trigger: FieldMoveTrigger = field(
        default_factory=FieldMoveTrigger, repr=False, compare=False
    )

    def destructive_reason(self, info: PromptInfo, goal_id: str | None) -> str | None:
        if self.authorize is None:
            return None
        reason = self.authorize(info, goal_id)
        return reason if reason and reason.strip() else None

    def serves_goal_for(self, info: PromptInfo, goal_id: str | None) -> bool | None:
        """Return the objective fact, falling back to the registered prompt default."""
        if self.serves_goal is not None:
            value = self.serves_goal(info, goal_id)
            if value is not None:
                return value
        trigger = self.field_move_trigger
        if (
            info.kind is PromptKind.FIELD_MOVE
            and trigger.goal_id == goal_id
            and trigger.move == info.field_move
        ):
            return trigger.serves_goal
        key = info.rule_kind or info.kind.value
        if key in self.default_answers:
            return bool(self.default_answers[key])
        return info.default_yes

    def set_field_move_trigger(
        self,
        *,
        goal_id: str | None,
        move: str,
        serves_goal: bool,
        next_route_step: bool = False,
        expected_destination: tuple[int, int] | None = None,
        on_decline: Callable[[], None] | None = None,
    ) -> None:
        self.field_move_trigger.goal_id = goal_id
        self.field_move_trigger.move = move
        self.field_move_trigger.serves_goal = serves_goal
        self.field_move_trigger.next_route_step = next_route_step
        self.field_move_trigger.expected_destination = expected_destination
        self.field_move_trigger.on_decline = on_decline

    def field_move_is_next_route_step(self, info: PromptInfo, goal_id: str | None) -> bool:
        trigger = self.field_move_trigger
        return (
            info.kind is PromptKind.FIELD_MOVE
            and trigger.goal_id == goal_id
            and trigger.move == info.field_move
            and trigger.next_route_step
        )

    def finish_field_move(self, *, goal_id: str | None, move: str, declined: bool) -> None:
        trigger = self.field_move_trigger
        if trigger.goal_id != goal_id or trigger.move != move:
            return
        if declined and trigger.on_decline is not None:
            trigger.on_decline()
        trigger.clear()
