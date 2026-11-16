"""A bound on the learn-move loop-back (the runtime contracts section
7, item 6).

The learn-move questions form a cycle: "Delete a move?" -NO-> "Stop learning?" -NO-> "Delete a
move?". Every answer is a legal option, so a decider that always picks the loop-back
(`keep_trying`) never leaves the scene (the runner's repeat guard only escalates after eight
identical repeats). This module counts how often the SAME question is entered inside one learn
session; the builder withdraws the loop-back option once the count passes
`SystemFlowOptions.learn_loop_limit` and says so in the option text. It chooses nothing: the
options that remain are the ones that end the loop.

Counting is by ENTRY, never by build: the executor rebuilds the frame of the same screen on its
fresh re-validation snapshot, and that must not count as a second visit. State is per run (one
builder).
"""

from __future__ import annotations

from collections.abc import Hashable


class QuestionCounter:
    def __init__(self) -> None:
        self._session: Hashable | None = None
        self._last: Hashable | None = None
        self._counts: dict[Hashable, int] = {}

    def reset(self) -> None:
        """Another scene was built: the learn session is over."""
        self._session = self._last = None
        self._counts.clear()

    def enter(self, session: Hashable, question: Hashable) -> int:
        """How many times ``question`` was entered in ``session`` including this one. A repeat build
        of the screen already on top (same question as the last call) does not count."""
        if session != self._session:
            self.reset()
            self._session = session
        if question != self._last:
            self._counts[question] = self._counts.get(question, 0) + 1
            self._last = question
        return self._counts.get(question, 1)
