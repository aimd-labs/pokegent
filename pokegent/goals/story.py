"""Bounded, opt-in authored progression over current RAM; no packaged game policy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from pokegent import local_data
from pokegent.goals.dex_fallback import FallbackUnavailable
from pokegent.goals.models import Objective, probe_leaves
from pokegent.goals.tracker import evaluate_probe
from pokegent.goals.validate import validate

Text = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Ids = Annotated[list[Text], Field(max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Provenance(_Strict):
    rom_sha256: Digest
    source_sha256: Digest
    map_index_sha256: Digest
    source_pin: Text
    generator: Text


class Policy(_Strict):
    order: Ids
    required: Annotated[list[Text], Field(min_length=1, max_length=64)]


class Node(_Strict):
    id: Text
    requires: Ids
    objective: Objective


class Progression(_Strict):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    provenance: Provenance
    policy: Policy
    policy_sha256: Digest
    nodes: Annotated[list[Node], Field(min_length=1, max_length=64)]


class StoryComplete(Exception):
    """All required nodes are proven complete in this observation only."""


def load_progression(*, path: Path | None = None, directory=None) -> StoryFallback:
    """Read one fixed external artifact, independently of the seven setup schemas."""
    try:
        if path is None:
            path = Path(os.environ[local_data.LOCAL_DATA_ENV]).expanduser() / "progression.json"
        path = path.resolve()
        if path.is_relative_to(local_data._CHECKOUT):
            raise ValueError("checkout data")
        if not path.is_file():
            raise ValueError("not a regular file")
        with path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("artifact too large")
        graph = Progression.model_validate_json(raw)
        policy = graph.policy.model_dump()
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"))
                                .encode()).hexdigest()
        if digest != graph.policy_sha256:
            raise ValueError("policy digest")
        nodes = {node.id: node for node in graph.nodes}
        order, required = graph.policy.order, graph.policy.required
        if (len(nodes) != len(graph.nodes) or len(set(order)) != len(order)
                or set(order) != set(nodes) or len(set(required)) != len(required)
                or not set(required) <= set(nodes)
                or len({node.objective.objective_id for node in graph.nodes}) != len(nodes)):
            raise ValueError("node identity")
        resolved = {}
        for node in graph.nodes:
            obj = node.objective
            if (len(set(node.requires)) != len(node.requires)
                    or not set(node.requires) <= set(nodes)
                    or not obj.success_probes or obj.expires_at or obj.next_review_on
                    or any(p.kind not in ("flag", "var", "var_at_least", "item")
                           for p in probe_leaves(obj.success_probes))):
                raise ValueError("unsupported node")
            verdict = validate(obj, directory=directory)
            if not verdict.ok:
                raise ValueError("objective invalid")
            resolved[node.id] = verdict.resolved
        visited, visiting = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("cycle")
            if key not in visited:
                visiting.add(key)
                for dependency in nodes[key].requires:
                    visit(dependency)
                visiting.remove(key)
                visited.add(key)

        for key in order:
            visit(key)
        return StoryFallback(graph, nodes, resolved)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise FallbackUnavailable("invalid progression artifact") from None


def _proof(probe, snapshot):
    if probe.kind == "any_of":
        values = [_proof(child, snapshot) for child in probe.children]
        return True if True in values else None if None in values else False
    if ((probe.kind == "flag" and snapshot.flags is None)
            or (probe.kind in ("var", "var_at_least") and snapshot.vars is None)):
        return None
    return evaluate_probe(probe, snapshot)


class StoryFallback:
    def __init__(self, graph, nodes, resolved):
        self.graph, self.nodes, self.resolved = graph, nodes, resolved

    def _states(self, snapshot):
        states = {}
        for key, resolved in self.resolved.items():
            values = ([_proof(probe, snapshot) for probe in resolved.probes]
                      if snapshot.usable else [None])
            states[key] = False if False in values else None if None in values else True
        return states

    def facts(self, snapshot):
        """Read-only prerequisite evidence; authored order is not derived readiness."""
        states = self._states(snapshot)
        order = self.graph.policy.order
        challenge = next((key for key in order if key in self.graph.policy.required
                          and states[key] is not True), None)
        relevant = set()

        def visit(key):
            for dependency in self.nodes[key].requires:
                if dependency not in relevant:
                    relevant.add(dependency)
                    # Persistent downstream proof discharges consumed earlier prerequisites.
                    if states[dependency] is not True:
                        visit(dependency)

        if challenge is not None:
            visit(challenge)
        labels = {}
        truncated = False
        for key in relevant | ({challenge} if challenge is not None else set()):
            obj = self.nodes[key].objective
            text = " ".join((obj.rationale or obj.kind).split())
            labels[key] = text[:80]
            truncated |= len(text) > 80

        def entry(key):
            return {"label": labels[key], "state": (
                "unknown" if states[key] is None else "proven" if states[key] else "unmet")}

        prerequisites = [key for key in order if key in relevant]
        counts = {name: sum(states[key] is value for key in relevant)
                  for name, value in (("proven", True), ("unmet", False), ("unknown", None))}
        result = {
            "schema": "story-facts/1", "status": (
                "complete" if challenge is None else "unknown"
                if states[challenge] is None or counts["unknown"] else "available"),
            "policy": "authored", "provenance": "declared",
            "readiness": "prerequisites_only",
            "next_challenge": entry(challenge) if challenge is not None else None,
            "prerequisites": [entry(key) for key in prerequisites[:6]],
            "counts": {**counts, "total": len(relevant)},
            "truncated": truncated or len(prerequisites) > 6,
        }
        while len(json.dumps(result).encode("utf-8")) > 1536:
            result["truncated"] = True
            if result["prerequisites"]:
                result["prerequisites"].pop()
            else:
                item = result["next_challenge"]
                item["label"] = item["label"][:len(item["label"]) // 2]
        return result

    def from_snapshot(self, snapshot):
        if not snapshot.usable:
            raise FallbackUnavailable("story requires usable RAM")
        states = self._states(snapshot)

        def frontier(key):
            if states[key] is None:
                raise FallbackUnavailable("story completion proof unknown")
            if states[key]:
                return None
            for dependency in self.graph.policy.order:
                if dependency in self.nodes[key].requires:
                    candidate = frontier(dependency)
                    if candidate is not None:
                        return candidate
            return key

        for key in self.graph.policy.order:
            if key in self.graph.policy.required:
                candidate = frontier(key)
                if candidate is not None:
                    return {**self.nodes[candidate].objective.model_dump(mode="json"),
                            "set_by": "fallback:story"}
        raise StoryComplete
