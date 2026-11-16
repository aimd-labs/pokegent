"""Cinnabar Gym quiz facts for the optional special family.

The dialog family owns the yes/no input.  This module keeps the ROM-backed question table in a
small, inspectable object so the six doors and their event flags cannot drift from the prompt
registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.local_data import read_data


@dataclass(frozen=True)
class QuizQuestion:
    number: int
    prompt: str
    answer_yes: bool
    door_flag: int


def _load() -> tuple[QuizQuestion, ...]:
    data = read_data("cinnabar_quiz.yaml")
    return tuple(
        QuizQuestion(
            number=int(row["number"]),
            prompt=str(row["prompt"]),
            answer_yes=bool(row["answer_yes"]),
            door_flag=int(str(row["door_flag"]), 0),
        )
        for row in data["questions"]
    )


QUESTIONS: tuple[QuizQuestion, ...] = (
    _load() if read_data("cinnabar_quiz.yaml", optional=True) is not None else ()
)
DOORS: dict[int, int] = {question.number: question.door_flag for question in QUESTIONS}


def facts(question: QuizQuestion, answer_yes: bool) -> dict[str, object]:
    """Return facts for a proposed answer; a wrong answer starts the trainer battle."""
    return {
        "question_kind": "quiz",
        "door": question.number,
        "door_flag": question.door_flag,
        "serves_goal": answer_yes == question.answer_yes,
        "wrong_answer_cost": "trainer battle",
    }


__all__ = ["DOORS", "QUESTIONS", "QuizQuestion", "facts"]
