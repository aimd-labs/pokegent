"""Opt-in readable diagnostics on the served frame stay bounded, redacted and inert."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from pokegent import server
from pokegent.options.models import DecisionFrame, Legality, Option, fingerprint

WAIT_TIMEOUT = 5.0


@pytest.fixture
def no_sockets(monkeypatch):
    monkeypatch.setattr(
        server,
        "ThreadingHTTPServer",
        lambda *args: SimpleNamespace(
            server_address=("127.0.0.1", 19760),
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        ),
    )


def _option(key, label=None):
    return Option(
        id=key,
        label=label or key,
        legality=Legality.LEGAL,
        executor_ref="test.action",
    )


def _diagnostic(key, reason=..., legality=Legality.UNKNOWN, **fields):
    facts = {} if reason is ... else {"reason": reason}
    return Option(
        id=f"diag:{key}",
        label=key,
        legality=legality,
        facts=facts,
        **fields,
    )


def _frame(*, options=("sign", "walk"), diagnostics=(), decision_id="dec-1", seq=7):
    return DecisionFrame(
        decision_id=decision_id,
        run_id="synthetic",
        snapshot_seq=seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=fingerprint("field.free"),
        goal_id="objective-1",
        options=tuple(_option(key) for key in options),
        diagnostics=tuple(diagnostics),
    )


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def _start_offer(external, frame, **kwargs):
    outcome = {}

    def run():
        try:
            outcome["result"] = external.offer(frame, **kwargs)
        except BaseException as exc:  # surfaced by the test, never swallowed
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_until(
        lambda: external._frame is not None and external._status == "waiting_for_choice"
    )
    return thread, outcome


def _finish(external, body, thread, outcome, option_id=None):
    external.choose(
        {
            "frame_id": body["frame_id"],
            "option_id": option_id or body["options"][0]["id"],
        }
    )
    thread.join(2)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")


def _http_get(external, path):
    replies = []
    request = SimpleNamespace(path=path, _send=lambda code, body: replies.append((code, body)))
    external._handler().do_GET(request)
    assert len(replies) == 1
    return replies[0]


def test_diagnostics_is_opt_in_on_both_api_and_http_route(no_sockets):
    external = server.ExternalDecider()
    frame = _frame(diagnostics=(_diagnostic("map_unreadable", "map state is unreadable"),))
    thread, outcome = _start_offer(external, frame, wait_timeout=WAIT_TIMEOUT)

    default = external.frame_response(wait_timeout=0.0)[1]
    opted = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]

    assert "diagnostics" not in default
    assert not [key for key in default if key.startswith("_")]
    assert set(opted) == set(default) | {"diagnostics"}
    assert opted["diagnostics"] == {
        "total": 1,
        "items": [{"legality": "UNKNOWN", "reason": "map state is unreadable"}],
    }
    assert set(opted["diagnostics"]["items"][0]) == {"legality", "reason"}
    # The opt-in changes neither identity nor the guarded state/options the caller sees.
    assert opted["frame_id"] == default["frame_id"]
    assert {key: value for key, value in opted.items() if key != "diagnostics"} == default

    assert _http_get(external, "/frame")[1].get("diagnostics") is None
    http_opted = _http_get(external, "/frame?diagnostics=1")
    assert http_opted[0] == 200
    assert http_opted[1]["diagnostics"] == opted["diagnostics"]

    _finish(external, default, thread, outcome)


def test_unknown_and_budget_reasons_are_verbatim_not_prerequisites(no_sockets):
    external = server.ExternalDecider()
    frame = _frame(
        diagnostics=(
            _diagnostic("map_unreadable", "map state is unreadable"),
            _diagnostic(
                "route_budget",
                "objective route search budget exhausted",
                legality=Legality.UNAVAILABLE,
            ),
        )
    )
    thread, outcome = _start_offer(external, frame, wait_timeout=WAIT_TIMEOUT)
    body = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]
    items = body["diagnostics"]["items"]

    assert items[0] == {"legality": "UNKNOWN", "reason": "map state is unreadable"}
    assert items[1] == {
        "legality": "UNAVAILABLE",
        "reason": "objective route search budget exhausted",
    }
    rendered = json.dumps(body["diagnostics"])
    assert "no party member knows" not in rendered
    assert "blocked_route" not in rendered
    _finish(external, body, thread, outcome)


def test_missing_reason_stays_null_and_selection_reason_is_not_substituted(no_sockets):
    external = server.ExternalDecider()
    no_reason = _diagnostic("unknown_effect")
    sourced = Option(
        id="diag:filtered",
        label="filtered",
        legality=Legality.UNAVAILABLE,
        facts={"reason": "the exit is closed", "selection_reason": "route_focus"},
    )
    thread, outcome = _start_offer(
        external, _frame(diagnostics=(no_reason, sourced)), wait_timeout=WAIT_TIMEOUT
    )
    items = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]["diagnostics"][
        "items"
    ]
    assert items[0] == {"legality": "UNKNOWN", "reason": None}
    assert items[1] == {"legality": "UNAVAILABLE", "reason": "the exit is closed"}
    _finish(external, external.frame_response(wait_timeout=0.0)[1], thread, outcome)


def test_diagnostic_rows_do_not_grant_action_authority(no_sockets):
    external = server.ExternalDecider()
    diagnostic = _diagnostic("map_unreadable", "map state is unreadable")
    thread, outcome = _start_offer(
        external, _frame(diagnostics=(diagnostic,)), wait_timeout=WAIT_TIMEOUT
    )
    default = external.frame_response(wait_timeout=0.0)[1]
    opted = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]

    offered = {item["id"] for item in (*default["options"], *default.get("all_options", ()))}
    assert diagnostic.id not in offered
    assert server._option_set_hash(default) == server._option_set_hash(opted)
    with pytest.raises(ValueError):
        external.choose({"frame_id": opted["frame_id"], "option_id": diagnostic.id})
    _finish(external, default, thread, outcome)
    assert outcome["result"].option_id == "sign"


def test_unoffered_executor_tokens_are_redacted_or_refused(no_sockets):
    external = server.ExternalDecider()
    redacted = Option(
        id="diag:token",
        label="token",
        legality=Legality.UNAVAILABLE,
        facts={"reason": "cannot use secret.executor here"},
        executor_ref="secret.executor",
    )
    refused = _diagnostic("pointer", "read from 0x08000000 failed")
    thread, outcome = _start_offer(
        external, _frame(diagnostics=(redacted, refused)), wait_timeout=WAIT_TIMEOUT
    )
    body = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]
    items = body["diagnostics"]["items"]
    rendered = json.dumps(body)

    assert "secret.executor" not in rendered
    assert "0x08000000" not in rendered
    assert items[0] == {
        "legality": "UNAVAILABLE",
        "reason": "cannot use [internal reference removed] here",
    }
    assert items[1] == {"legality": "UNKNOWN", "reason": None}
    _finish(external, body, thread, outcome)


def test_count_and_reason_length_are_bounded(no_sockets):
    external = server.ExternalDecider()
    diagnostics = tuple(
        _diagnostic(f"row_{index}", "x" * 500) for index in range(40)
    )
    thread, outcome = _start_offer(
        external, _frame(diagnostics=diagnostics), wait_timeout=WAIT_TIMEOUT
    )
    document = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]["diagnostics"]

    assert document["total"] == 40
    assert len(document["items"]) == 32
    assert all(len(item["reason"]) <= 240 for item in document["items"])
    assert all(item["reason"].endswith("...") for item in document["items"])
    assert len(json.dumps(document)) < 32 * 300
    _finish(external, external.frame_response(wait_timeout=0.0)[1], thread, outcome)


def test_refresh_rebinds_rows_to_the_new_frame_id(no_sockets):
    external = server.ExternalDecider()
    original = _frame(diagnostics=(_diagnostic("old", "old reason"),))
    refreshed = _frame(
        options=("sign", "talk"),
        diagnostics=(_diagnostic("new", "new reason"),),
        decision_id="dec-2",
    )

    def refresh(_frame):
        if refresh.done:
            return None
        refresh.done = True
        return refreshed

    refresh.done = False
    thread, outcome = _start_offer(external, original, wait_timeout=WAIT_TIMEOUT)
    first = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]
    assert first["diagnostics"]["items"] == [
        {"legality": "UNKNOWN", "reason": "old reason"}
    ]

    external.bind_pending_frame(refresh)
    _wait_until(
        lambda: external._frame is not None
        and external._frame["frame_id"] != first["frame_id"]
    )
    second = external.frame_response(diagnostics=True, wait_timeout=0.0)[1]

    assert second["frame_id"] != first["frame_id"]
    assert second["diagnostics"] == {
        "total": 1,
        "items": [{"legality": "UNKNOWN", "reason": "new reason"}],
    }
    assert "old reason" not in json.dumps(second["diagnostics"])
    _finish(external, second, thread, outcome, option_id="talk")
