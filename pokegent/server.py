"""Loopback HTTP decider for agents that are not bundled with pokegent."""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pokegent.dashboard.stream.frames import FrameHub
from pokegent.options.models import (
    OBJECTIVE_RELATION_NOT_SERVING,
    ChoiceResult,
    DeciderTimeout,
    DecisionFrame,
    assert_payload_clean,
    sanitize_internal_text,
    to_system_one_payload,
)
from pokegent.state.briefing import BRIEFING_MAX_BYTES, build_briefing, trim_briefing

FILTERED_REASONS = frozenset(
    {
        "local_objective_focus",
        "route_focus",
        "alternating_without_progress",
        "closed",
    }
)
NO_EFFECT_HIDDEN_PREFIX = "hidden: repeated with no effect "
BLOCKED_DIALOG_PREFIX = "blocked by the same dialog "
_DEFAULT_WAIT = object()


def _filtered_reason(option: Any) -> str:
    facts = option.facts
    reason = facts.get("selection_reason")
    # Field enrichment keeps the builder's reason under ``focus_filtered`` when it restores a
    # hidden option's original lineage.
    if not (
        reason in FILTERED_REASONS
        or (isinstance(reason, str) and reason.startswith(NO_EFFECT_HIDDEN_PREFIX))
        or (isinstance(reason, str) and reason.startswith(BLOCKED_DIALOG_PREFIX))
    ):
        reason = facts.get("focus_filtered")
    if reason not in FILTERED_REASONS and facts.get("objective_relation") == (
        OBJECTIVE_RELATION_NOT_SERVING
    ):
        reason = "local_objective_focus"
    return (
        reason
        if reason in FILTERED_REASONS
        or (isinstance(reason, str) and reason.startswith(NO_EFFECT_HIDDEN_PREFIX))
        or (isinstance(reason, str) and reason.startswith(BLOCKED_DIALOG_PREFIX))
        else "situational_focus"
    )


MAX_OBJECTIVE_BODY = 16 * 1024
BODY_READ_TIMEOUT = 5.0


def _json_bytes(value: Any) -> bytes:
    # Keep the response encoding used before the planning API: in particular, json.dumps's
    # ensure_ascii=True default and its default separators are part of existing clients' output.
    return (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


_DROP_FIELD = object()


def _clean_briefing_fields(value: Any, frame: DecisionFrame) -> Any:
    """Sanitize readable values and drop any field that still fails the leak guard."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "nickname" and isinstance(item, str):
                item = sanitize_internal_text(item, frame)
            candidate = _clean_briefing_fields(item, frame)
            if candidate is _DROP_FIELD:
                continue
            try:
                assert_payload_clean(
                    {"state": {str(key): candidate}, "instructions": "", "criteria": {}},
                    frame,
                )
            except Exception:
                continue
            cleaned[str(key)] = candidate
        return cleaned
    if isinstance(value, list):
        return [
            candidate
            for item in value
            if (candidate := _clean_briefing_fields(item, frame)) is not _DROP_FIELD
        ]
    try:
        assert_payload_clean({"state": {"value": value}, "instructions": "", "criteria": {}}, frame)
    except Exception:
        return _DROP_FIELD
    return value


class ExternalDecider:
    """A blocking decider served by a localhost-only HTTP endpoint."""

    external_control = True
    supervisor_control = True

    def __init__(
        self,
        port: int = 19760,
        timeout: float = 300.0,
        frame_wait_timeout: float = 30.0,
        *,
        autoplay: str = "off",
    ) -> None:
        if not 19700 <= port <= 19799:
            raise ValueError("external port must be within 19700-19799")
        if timeout <= 0:
            raise ValueError("external timeout must be > 0")
        if frame_wait_timeout <= 0:
            raise ValueError("frame wait timeout must be > 0")
        if autoplay != "off":
            raise ValueError("autoplay=forced was removed: the decider owns action selection")
        self.timeout = timeout
        self.frame_wait_timeout = frame_wait_timeout
        self._autoplay = autoplay
        self._condition = threading.Condition()
        self._frame: dict[str, Any] | None = None
        self._choice: str | None = None
        self._decided_frame: DecisionFrame | None = None
        self._outcome: dict[str, Any] | None = None
        self._feedback: list[dict[str, Any]] | None = None
        self._actions_since_decision: list[dict[str, Any]] = []
        self._agent_choice_pending = False
        self._autoplay_pending: dict[str, Any] | None = None
        self._has_external_decision = False
        self._status = "starting"
        self._stop_reason: str | None = None
        self._interrupt_requested = threading.Event()
        self._stop_requested = threading.Event()
        self._macro_running = False
        self._interruptible_wait = False
        self._sequence = 0
        self._frames = FrameHub(None)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self._server.server_address[1]
        self._briefing: dict[str, Any] = {"schema": "briefing/1", "summary": "No game frame yet."}
        self._objective_store: Any = None
        self._objective_status: Any = None
        self._objective_status_cache: dict[str, Any] = {}
        self._objective_status_refreshing = False
        self._pending_frame_refresh: Any = None
        self._agent_request_observer: Any = None
        self._planning_lock = threading.RLock()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._status = "running"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _write_response(self, code: int, body: bytes, content_type: str) -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    logging.getLogger(__name__).debug("client disconnected while sending response")

            def _send(self, code: int, value: Any) -> None:
                body = _json_bytes(value)
                self._write_response(code, body, "application/json")

            def _authorized(self) -> bool:
                """Allow only loopback Host and a same-origin browser request."""
                host_header = self.headers.get("Host", "")
                host_name = ""
                try:
                    host_name = urlsplit(f"//{host_header}").hostname or ""
                    host = ipaddress.ip_address(host_name)
                except ValueError:
                    host_name = host_name.lower() if "host_name" in locals() else ""
                    host = None if host_name != "localhost" else host_name
                if host is None or (host != "localhost" and not host.is_loopback):
                    self._send(403, {"error": "loopback Host required"})
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    origin_name = ""
                    try:
                        origin_parts = urlsplit(origin)
                        origin_name = (origin_parts.hostname or "").lower()
                        origin_host = ipaddress.ip_address(origin_name)
                    except ValueError:
                        origin_parts = None
                        origin_host = "localhost" if origin_name == "localhost" else None
                    if (
                        origin_parts is None
                        or origin_parts.scheme != "http"
                        or origin_host is None
                        or (origin_host != "localhost" and not origin_host.is_loopback)
                        or origin_parts.netloc.lower() != host_header.lower()
                    ):
                        self._send(403, {"error": "same-origin loopback request required"})
                        return False
                return True

            def _read_json_body(self) -> Any:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValueError("Content-Length is required")
                try:
                    length = int(raw_length)
                except (TypeError, ValueError):
                    raise ValueError("Content-Length must be an integer") from None
                if length < 0:
                    raise ValueError("Content-Length must not be negative")
                if length > MAX_OBJECTIVE_BODY:
                    raise OverflowError("objective body is too large")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ValueError("Content-Type must be application/json")
                previous_timeout = self.connection.gettimeout()
                self.connection.settimeout(BODY_READ_TIMEOUT)
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    raise ValueError(f"request body read timed out: {exc}") from None
                finally:
                    self.connection.settimeout(previous_timeout)
                return json.loads(body)

            def do_GET(self) -> None:  # noqa: N802
                from urllib.parse import parse_qs, urlsplit

                parsed = urlsplit(self.path)
                if parsed.path == "/screenshot":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    try:
                        scale = int(query.get("scale", ["1"])[0])
                    except ValueError:
                        self._send(400, {"error": "scale must be an integer from 1 to 4"})
                        return
                    if not 1 <= scale <= 4:
                        self._send(400, {"error": "scale must be an integer from 1 to 4"})
                        return
                    # The agent may arrive after demand-driven capture went idle. Refresh
                    # through the read-only source so a pending choice sees the current screen.
                    frame = owner._frames.grab(max_age=0)
                    if frame is None:
                        self._send(503, {"error": "no frame yet"})
                        return
                    try:
                        body = frame.png(scale)
                    except Exception as exc:  # noqa: BLE001 - isolate encoder failures
                        self._send(500, {"error": f"could not encode screenshot: {exc}"})
                        return
                    self._write_response(200, body, "image/png")
                    return
                from urllib.parse import parse_qs, urlsplit

                parsed = urlsplit(self.path)
                if parsed.path == "/frame":
                    raw_wait = parse_qs(parsed.query).get("wait", [None])[0]
                    try:
                        wait = None if raw_wait is None else float(raw_wait)
                    except (TypeError, ValueError):
                        self._send(400, {"error": "wait must be a number between 0 and 30"})
                        return
                    if wait is not None and not 0 <= wait <= 30:
                        self._send(400, {"error": "wait must be a number between 0 and 30"})
                        return
                    self._send(
                        *owner.frame_response(
                            all_options=parse_qs(parsed.query).get("all") == ["1"],
                            wait_timeout=wait,
                        )
                    )
                    return
                if parsed.path == "/status":
                    with owner._condition:
                        status = owner._status
                        stop_reason = owner._stop_reason
                        autoplay = owner._autoplay
                    owner._refresh_objective_status()
                    with owner._condition:
                        objective_status = dict(owner._objective_status_cache)
                    self._send(
                        200,
                        {
                            "status": status,
                            "stop_reason": stop_reason,
                            "autoplay": autoplay,
                            **objective_status,
                        },
                    )
                    return
                if parsed.path == "/briefing":
                    if not self._authorized():
                        return
                    with owner._planning_lock:
                        value = owner.briefing_response()
                    self._send(200, value)
                    return
                if parsed.path == "/objective":
                    if not self._authorized():
                        return
                    with owner._planning_lock:
                        value = owner.objective_response()
                    self._send(200, value)
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/interrupt":
                    if not self._authorized():
                        return
                    self._send(202, owner.interrupt())
                    return
                if self.path == "/objective":
                    if not self._authorized():
                        return
                    try:
                        body = self._read_json_body()
                    except OverflowError as exc:
                        self._send(413, {"accepted": False, "reason": str(exc)})
                        return
                    except Exception as exc:  # malformed framing/JSON must remain JSON
                        self._send(400, {"accepted": False, "reason": str(exc)})
                        return
                    if not isinstance(body, dict):
                        self._send(
                            400,
                            {"accepted": False, "reason": "objective requires one JSON object"},
                        )
                        return
                    try:
                        with owner._planning_lock:
                            result = owner.objective_propose(body)
                    except Exception as exc:  # propose must never escape as an HTML 500
                        self._send(500, {"accepted": False, "reason": str(exc)})
                        return
                    self._send(200 if result.get("accepted") else 422, result)
                    return
                if self.path != "/choose":
                    self._send(404, {"error": "not found"})
                    return
                if not self._authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length))
                    result = owner.choose(body)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                except LookupError as exc:
                    self._send(409, {"error": str(exc)})
                    return
                self._send(202, result)

            def do_DELETE(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != "/objective":
                    self._send(404, {"error": "not found"})
                    return
                if not self._authorized():
                    return
                from urllib.parse import parse_qs

                query = parse_qs(parsed.query)
                raw_revision = query.get("expected_revision", [None])[0]
                try:
                    expected_revision = int(raw_revision) if raw_revision is not None else None
                except ValueError:
                    self._send(400, {"cleared": False, "reason": "bad_expected_revision"})
                    return
                set_by = query.get("set_by", [None])[0]
                with owner._planning_lock:
                    result = owner.objective_clear(
                        expected_revision=expected_revision, set_by=set_by
                    )
                self._send(200 if result.get("cleared") else 409, result)

        return Handler

    def _wait_for_choice(
        self,
        frame: DecisionFrame,
        *,
        metadata: dict[str, Any] | None = None,
        default: ChoiceResult | None = None,
        wait_timeout: float | None | object = _DEFAULT_WAIT,
        interruptible_wait: bool = False,
    ) -> ChoiceResult:
        payload = to_system_one_payload(frame)
        assert_payload_clean(payload, frame)
        with self._condition:
            if self._stop_requested.is_set():
                raise DeciderTimeout("external choice cancelled")
            # Include autonomous work that happened before the first agent choice as well as
            # work since the previous frame.  The agent must be able to account for every action
            # it did not explicitly request.
            self._feedback = list(self._actions_since_decision) or None
            self._actions_since_decision.clear()
            self._sequence += 1
            frame_id = f"external-{self._sequence}"
            options = [
                {"id": option_id, "criterion": criterion}
                for option_id, criterion in payload["criteria"].items()
            ]
            served_frame = {
                "frame_id": frame_id,
                "scene": payload["state"].get("scene"),
                "objective": payload["state"].get("objective"),
                "state": payload["state"],
                "question": payload["state"].get("prompt"),
                "instructions": payload["instructions"],
                "options": options,
                "feedback": self._feedback,
            }
            if frame.all_options:
                all_frame = frame.model_copy(
                    update={
                        "options": frame.all_chooseable(),
                        "diagnostics": tuple(
                            option
                            for option in frame.diagnostics
                            if option.id not in frame.all_chooseable_ids()
                        ),
                        "all_options": (),
                    }
                )
                all_payload = to_system_one_payload(all_frame)
                assert_payload_clean(all_payload, all_frame)
                served_frame["all_options"] = [
                    {
                        "id": option.id,
                        "criterion": all_payload["criteria"][option.id],
                        "filtered_reason": _filtered_reason(option),
                    }
                    for option in frame.all_options
                ]
                for item in served_frame["all_options"]:
                    assert_payload_clean(
                        {
                            "state": {"filtered_reason": item["filtered_reason"]},
                            "instructions": "The controller may choose a legal option.",
                            "criteria": {},
                        },
                        frame,
                    )
            if metadata:
                served_frame.update(metadata)
                metadata_payload = dict(payload)
                metadata_payload["state"] = {
                    **payload["state"],
                    "_hybrid_metadata": metadata,
                }
                assert_payload_clean(metadata_payload, frame)
            self._frame = served_frame
            self._interrupt_requested.clear()
            self._macro_running = False
            self._choice = None
            self._outcome = None
            self._status = "waiting_for_choice"
            self._interruptible_wait = interruptible_wait
            self._condition.notify_all()
            observer = self._agent_request_observer
            if observer is not None and interruptible_wait:
                self._condition.release()
                try:
                    self._notify_agent_request(observer, frame_id, frame)
                finally:
                    self._condition.acquire()
            deadline = (
                time.monotonic() + self.timeout
                if wait_timeout is _DEFAULT_WAIT
                else None
                if wait_timeout is None
                else time.monotonic() + wait_timeout
            )
            while self._choice is None:
                if self._stop_requested.is_set():
                    self._frame = None
                    self._status = "stopped"
                    self._stop_reason = (
                        "external choice interrupted"
                        if self._interruptible_wait
                        else "external choice cancelled"
                    )
                    self._interruptible_wait = False
                    self._condition.notify_all()
                    raise DeciderTimeout(self._stop_reason)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    if default is not None:
                        self._status = "running"
                        self._frame = None
                        self._interruptible_wait = False
                        self._condition.notify_all()
                        return default
                    self._status = "safe_pause"
                    self._stop_reason = "external choice timed out"
                    self._frame = None
                    self._interruptible_wait = False
                    self._condition.notify_all()
                    raise DeciderTimeout(self._stop_reason)
                refresh = self._pending_frame_refresh
                served_frame_id = self._frame["frame_id"] if self._frame is not None else None
                if callable(refresh):
                    # Rebuilding may re-plan a route and take seconds. Never hold the condition
                    # while invoking runner-owned refresh code; a concurrent choice wins.
                    self._condition.release()
                    try:
                        try:
                            updated = refresh(frame)
                        except Exception:
                            updated = None
                        updated_payload = None
                        updated_all_options = None
                        if updated is not None:
                            updated_payload = to_system_one_payload(updated)
                            assert_payload_clean(updated_payload, updated)
                            if updated.all_options:
                                all_frame = updated.model_copy(
                                    update={
                                        "options": updated.all_chooseable(),
                                        "diagnostics": tuple(
                                            option
                                            for option in updated.diagnostics
                                            if option.id not in updated.all_chooseable_ids()
                                        ),
                                        "all_options": (),
                                    }
                                )
                                all_payload = to_system_one_payload(all_frame)
                                updated_all_options = [
                                    {
                                        "id": option.id,
                                        "criterion": all_payload["criteria"][option.id],
                                        "filtered_reason": _filtered_reason(option),
                                    }
                                    for option in updated.all_options
                                ]
                    finally:
                        self._condition.acquire()
                    still_waiting = (
                        updated_payload is not None
                        and self._choice is None
                        and self._status == "waiting_for_choice"
                        and self._frame is not None
                        and self._frame.get("frame_id") == served_frame_id
                    )
                    if still_waiting:
                        frame = updated
                        self._sequence += 1
                        self._frame["frame_id"] = f"external-{self._sequence}"
                        self._frame["objective"] = updated_payload["state"].get("objective")
                        self._frame["state"] = updated_payload["state"]
                        self._frame["question"] = updated_payload["state"].get("prompt")
                        self._frame["instructions"] = updated_payload["instructions"]
                        self._frame["options"] = [
                            {"id": option_id, "criterion": criterion}
                            for option_id, criterion in updated_payload["criteria"].items()
                        ]
                        if updated_all_options is None:
                            self._frame.pop("all_options", None)
                        else:
                            self._frame["all_options"] = updated_all_options
                        self._condition.notify_all()
                        new_frame_id = self._frame["frame_id"]
                        self._condition.release()
                        try:
                            self._notify_agent_request(observer, new_frame_id, frame)
                        finally:
                            self._condition.acquire()
                self._condition.wait(min(remaining, 0.1) if remaining is not None else 0.1)
            choice = self._choice
            self._decided_frame = frame
            self._agent_choice_pending = True
            self._has_external_decision = True
            self._frame = None
            self._status = "running"
            self._interruptible_wait = False
            return ChoiceResult(
                option_id=choice, provider_id="agent", decision_id=frame.decision_id
            )

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        return self._wait_for_choice(frame, wait_timeout=None, interruptible_wait=True)

    def bind_planning(self, store: Any) -> None:
        self._objective_store = store
        # Rebinding replaces the early, schema-only store with the runner's fully configured
        # store (including map-name validation). Re-read now so an early accepted objective is
        # subjected to the same semantic validation before it becomes active.
        current = getattr(store, "current", None)
        if callable(current):
            current()

    def bind_pending_frame(self, refresh: Any) -> None:
        """Bind a same-snapshot frame refresh used while an agent is choosing."""
        self._pending_frame_refresh = refresh

    def bind_agent_request(self, observer: Any) -> None:
        """Observe an agent frame after it is published, before waiting for its choice."""
        self._agent_request_observer = observer

    def take_decided_frame(self) -> DecisionFrame | None:
        """Return the frame atomically paired with the most recent accepted choice."""
        with self._condition:
            frame = self._decided_frame
            self._decided_frame = None
            return frame

    def bind_objective_status(self, status: Any) -> None:
        self._objective_status = status
        if callable(status):
            try:
                value = dict(status())
            except Exception:
                value = {}
            with self._condition:
                self._objective_status_cache = value

    def update_briefing(
        self,
        snapshot: Any,
        tables: Any = None,
        world: Any = None,
        events: list[dict[str, Any]] | None = None,
        frame: DecisionFrame | None = None,
    ) -> None:
        try:
            objective = (
                self._objective_store.current() if self._objective_store is not None else None
            )
            briefing = build_briefing(
                snapshot, tables=tables, world=world, objective=objective, events=events,
                trim=False,
                story_facts=frame.context.get("story_facts") if frame is not None else None,
            )
            if frame is not None:
                keys = briefing.get("keys")
                readable = {key: value for key, value in briefing.items() if key != "keys"}
                readable = sanitize_internal_text(readable, frame)
                cleaned = _clean_briefing_fields(readable, frame)
                briefing = cleaned if isinstance(cleaned, dict) else {}
                if isinstance(keys, dict):
                    briefing["keys"] = keys
            briefing = trim_briefing(briefing, max_bytes=BRIEFING_MAX_BYTES)
            with self._condition:
                self._briefing = briefing
        except Exception as exc:  # a planning view must never stop the run loop
            logging.getLogger(__name__).warning("briefing update failed: %s", exc)

    def objective_response(self) -> dict[str, Any]:
        if self._objective_store is None:
            return {"active": None, "revision": 0}
        obj = self._objective_store.current()
        return {
            "active": obj.model_dump(mode="json") if obj else None,
            "revision": self._objective_store.revision(),
        }

    def briefing_response(self) -> dict[str, Any]:
        """Return the latest RAM view with a just-in-time objective read."""
        briefing = dict(self._briefing)
        if self._objective_store is not None:
            briefing["objective"] = self.objective_response()["active"]
        return briefing

    def objective_propose(self, body: Any) -> dict[str, Any]:
        if self._objective_store is None:
            return {"accepted": False, "reason": "objective store unavailable"}
        if not isinstance(body, dict):
            raise ValueError("objective requires one JSON object")
        result = self._objective_store.propose(body)
        return {
            "accepted": result.accepted,
            "proposal": result.as_dict(),
            "active": result.objective.model_dump(mode="json") if result.objective else None,
        }

    def objective_clear(
        self, *, expected_revision: int | None = None, set_by: str | None = None
    ) -> dict[str, Any]:
        if self._objective_store is None:
            return {"cleared": False, "reason": "objective store unavailable"}
        return self._objective_store.clear(expected_revision=expected_revision, set_by=set_by)

    def offer(
        self,
        frame: DecisionFrame,
        *,
        metadata: dict[str, Any] | None = None,
        default: ChoiceResult | None = None,
        wait_timeout: float | None | object = _DEFAULT_WAIT,
        interruptible_wait: bool = False,
    ) -> ChoiceResult:
        """Expose a frame to an agent, optionally returning a local default on timeout."""
        return self._wait_for_choice(
            frame,
            metadata=metadata,
            default=default,
            wait_timeout=wait_timeout,
            interruptible_wait=interruptible_wait,
        )

    def frame_response(
        self, *, all_options: bool = False, wait_timeout: float | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Return the current frame, long-polling across perception transitions."""
        with self._condition:
            timeout = self.frame_wait_timeout if wait_timeout is None else wait_timeout
            deadline = time.monotonic() + timeout
            while self._frame is None and self._status not in {"safe_pause", "stopped"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._frame is not None:
                result = dict(self._frame)
                if not all_options:
                    result.pop("all_options", None)
                else:
                    result.setdefault("all_options", [])
                status_result = (200, result)
            else:
                status = self._status
                stop_reason = self._stop_reason
                autoplay = self._autoplay
                status_result = None
        if status_result is not None:
            self._refresh_objective_status()
            return status_result
        self._refresh_objective_status()
        with self._condition:
            objective_status = dict(self._objective_status_cache)
        return 200, {
            "status": status,
            "stop_reason": stop_reason,
            "autoplay": autoplay,
            **objective_status,
        }

    def _refresh_objective_status(self) -> None:
        if not callable(self._objective_status):
            return
        with self._condition:
            if self._objective_status_refreshing:
                return
            self._objective_status_refreshing = True
        try:
            value = dict(self._objective_status())
        except Exception:
            value = {}
        finally:
            with self._condition:
                if "value" in locals():
                    self._objective_status_cache = value
                self._objective_status_refreshing = False

    @staticmethod
    def _notify_agent_request(observer: Any, frame_id: str, frame: DecisionFrame) -> None:
        try:
            observer(frame_id, frame)
        except Exception:
            logging.getLogger(__name__).debug("agent request observer failed", exc_info=True)

    def choose(self, body: Any) -> dict[str, Any]:
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("frame_id"), str)
            or not isinstance(body.get("option_id"), str)
        ):
            raise ValueError("choose requires frame_id and option_id")
        with self._condition:
            if self._frame is None or body["frame_id"] != self._frame["frame_id"]:
                raise LookupError("stale frame")
            allowed = {option["id"] for option in self._frame["options"]}
            allowed.update(option["id"] for option in self._frame.get("all_options", ()))
            if body["option_id"] not in allowed:
                raise ValueError("option_id is not in the current frame")
            self._choice = body["option_id"]
            self._condition.notify_all()
            return {
                "accepted": True,
                "frame_id": body["frame_id"],
                "option_id": body["option_id"],
                "status": "queued",
            }

    def record_action_result(
        self, _frame: DecisionFrame, result: Any, *, decided_by: str | None = None
    ) -> None:
        with self._condition:
            pending = self._autoplay_pending
            try:
                status = result.status.value
                outcome = {
                    "option_id": result.option_id,
                    "status": status,
                    "outcome": (
                        "applied"
                        if status == "applied"
                        else "interrupted"
                        if status == "interrupted"
                        else "failed"
                    ),
                    "applied": status == "applied",
                    "verified": result.verified_outcome,
                    "reason": result.reason,
                    "agent_choice": self._agent_choice_pending,
                    "decided_by": decided_by
                    or (pending or {}).get("decided_by")
                    or ("agent" if self._agent_choice_pending else "autoplay"),
                    "interrupted": status == "interrupted",
                }
                if result.dialog_text:
                    captured_pages = {
                        page
                        for prior in self._actions_since_decision
                        for page in prior.get("dialog_text", ())
                    }
                    dialog_text = [
                        page for page in result.dialog_text if page not in captured_pages
                    ]
                    if dialog_text:
                        outcome["dialog_text"] = dialog_text
                if pending is not None:
                    outcome["why"] = pending["reason"]
                self._outcome = outcome
                self._actions_since_decision.append(outcome)
                self._agent_choice_pending = False
                self._condition.notify_all()
            finally:
                self._autoplay_pending = None
                self._interrupt_requested.clear()
                self._macro_running = False

    def mark_autoplay(
        self, _frame: DecisionFrame, option_id: str, reason: str, *, decided_by: str = "autoplay"
    ) -> None:
        with self._condition:
            self._autoplay_pending = {
                "option_id": option_id,
                "reason": reason,
                "decided_by": decided_by,
            }

    def clear_autoplay_pending(self) -> None:
        with self._condition:
            self._autoplay_pending = None

    def interrupt(self) -> dict[str, Any]:
        with self._condition:
            if self._interruptible_wait:
                self._stop_requested.set()
                self._condition.notify_all()
                return {"accepted": True, "status": "interrupt_requested"}
            if not self._macro_running:
                return {"accepted": False, "status": "no_action_running"}
            self._interrupt_requested.set()
            self._condition.notify_all()
            return {"accepted": True, "status": "interrupt_requested"}

    def cancel(self) -> None:
        """Wake a pending choice without changing the supervisor's macro interrupt flag."""
        self._stop_requested.set()
        with self._condition:
            self._condition.notify_all()

    def interrupt_requested(self) -> bool:
        return self._interrupt_requested.is_set()

    def clear_interrupt(self) -> None:
        self._interrupt_requested.clear()

    def macro_started(self) -> None:
        with self._condition:
            self._interrupt_requested.clear()
            self._macro_running = True

    def macro_finished(self) -> None:
        with self._condition:
            # Keep the action live until its result is recorded.  This closes the small race
            # between the final safe boundary and Runner.record_action_result; publishing the
            # next frame (or recording the result) makes the following interrupt a no-op.
            self._interrupt_requested.clear()

    def set_autoplay(self, mode: str) -> None:
        if mode != "off":
            raise ValueError("autoplay=forced was removed: the decider owns action selection")
        with self._condition:
            self._autoplay = mode

    def close(self) -> None:
        self.cancel()
        with self._condition:
            self._status = "stopped"
            self._stop_reason = "run ended"
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._frames.stop()

    def attach_screen_source(self, source: Any) -> None:
        self._frames.attach(source)
        self._frames.touch()


def request(port: int, path: str, *, body: dict[str, str] | None = None) -> Any:
    data = _json_bytes(body) if body is not None else None
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=35) as response:
        return json.loads(response.read())


def request_bytes(port: int, path: str) -> bytes:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=35) as response:
        return response.read()
