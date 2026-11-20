"""`step choose` frame binding: legacy current-frame refetch vs exact `--frame-id`."""

import io
import json
from contextlib import closing
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from pokegent import cli, server


@pytest.fixture
def bridge(monkeypatch):
    # Same in-process transport seam as tests/test_agent_onboarding.py: the real
    # pokegent.server.request client talks to the decider's own handler, no sockets.
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
    external = server.ExternalDecider()
    calls = []

    def urlopen(req, timeout):
        assert req.host == "127.0.0.1:19760" and timeout == 35
        calls.append(req)
        handler = object.__new__(external._handler())
        parsed = urlsplit(req.full_url)
        handler.path = parsed.path + ("?" + parsed.query if parsed.query else "")
        handler.headers = {
            "Host": req.host,
            "Content-Length": str(len(req.data or b"")),
            "Content-Type": req.get_header("Content-type", ""),
        }
        handler.rfile = io.BytesIO(req.data or b"")
        responses = []
        handler._send = lambda code, body: responses.append((code, body))
        getattr(handler, "do_" + req.get_method())()
        code, body = responses.pop()
        wire = json.dumps(body).encode()
        if code >= 400:
            raise HTTPError(req.full_url, code, str(body), {}, io.BytesIO(wire))
        return closing(io.BytesIO(wire))

    monkeypatch.setattr(server, "urlopen", urlopen)
    yield external, calls
    external.cancel()
    external.close()


def _publish(external, frame_id, option_ids):
    with external._condition:
        external._frame = {
            "frame_id": frame_id,
            "options": [{"id": option_id, "criterion": option_id} for option_id in option_ids],
        }
        external._status = "waiting_for_choice"
        external._condition.notify_all()


def _paths(calls):
    return [urlsplit(req.full_url).path for req in calls]


def test_explicit_frame_id_posts_exact_inspected_frame(bridge, monkeypatch, capsys):
    external, calls = bridge
    _publish(external, "external-A", ("move:1",))
    monkeypatch.setattr("sys.argv", ["pokegent", "step", "choose", "move:1",
                                     "--frame-id", "external-A"])
    cli.main()
    assert _paths(calls) == ["/choose"]  # no frame GET, before or after
    assert json.loads(calls[0].data) == {"frame_id": "external-A", "option_id": "move:1"}
    assert external._choice == "move:1"
    assert external._frame["frame_id"] == "external-A"
    assert '"accepted": true' in capsys.readouterr().out


def test_explicit_frame_id_stays_on_inspected_frame_with_same_option(bridge, monkeypatch,
                                                                     capsys):
    # Inspected A; the bridge advanced to B, which still offers the same option.
    # The exact id must not be rebound to B, and the server 409 must survive.
    external, calls = bridge
    _publish(external, "external-A", ("move:1",))
    with external._condition:
        external._frame = {
            "frame_id": "external-B",
            "options": [{"id": "move:1", "criterion": "move:1"}],
        }
    monkeypatch.setattr("sys.argv", ["pokegent", "step", "choose", "move:1",
                                     "--frame-id", "external-A"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "stale frame" in capsys.readouterr().err  # the server's 409 body
    assert _paths(calls) == ["/choose"]
    assert json.loads(calls[0].data) == {"frame_id": "external-A", "option_id": "move:1"}
    assert external._choice is None
    assert external._frame["frame_id"] == "external-B"


def test_legacy_choose_refetches_current_frame_and_prints_it(bridge, monkeypatch, capsys):
    external, calls = bridge
    _publish(external, "external-A", ("move:1",))
    monkeypatch.setattr("sys.argv", ["pokegent", "step", "--wait", "0", "choose", "move:1"])
    cli.main()
    assert _paths(calls) == ["/frame", "/choose", "/frame"]
    assert "all=1" in urlsplit(calls[0].full_url).query
    assert json.loads(calls[1].data) == {"frame_id": "external-A", "option_id": "move:1"}
    assert external._choice == "move:1"
    assert '"frame_id": "external-A"' in capsys.readouterr().out


def test_choose_help_documents_frame_id(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pokegent", "step", "choose", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "--frame-id" in capsys.readouterr().out
