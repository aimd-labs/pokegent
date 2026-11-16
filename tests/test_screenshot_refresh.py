"""The agent screenshot endpoint refreshes a synthetic read-only framebuffer."""

from types import SimpleNamespace

from PIL import Image

from pokegent.dashboard.stream.frames import FrameHub
from pokegent.server import ExternalDecider


def test_agent_screenshot_refreshes_after_capture_went_idle():
    # Exercise the real HTTP handler with a synthetic read-only screen source, without sockets.
    source = SimpleNamespace(frames_run=0)
    source.read_screen = lambda: SimpleNamespace(
        frame=source.frames_run,
        image=Image.new("RGB", (240, 160), (source.frames_run, 0, 0)),
    )
    bridge = ExternalDecider.__new__(ExternalDecider)
    bridge._frames = FrameHub(source)
    handler = bridge._handler()
    replies = []
    request = SimpleNamespace(path="/screenshot",
                              _write_response=lambda *args: replies.append(args))
    handler.do_GET(request)
    source.frames_run = 1
    handler.do_GET(request)
    assert all(code == 200 and mime == "image/png" for code, _, mime in replies)
    assert replies[0][1] != replies[1][1]
    assert bridge._frames.latest().emu_frame == 1
