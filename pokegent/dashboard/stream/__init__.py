"""Optional local stream addon.

The stream addon owns framebuffer and audio capture.  It has no dependency on
the operator dashboard, so a run can expose only the game picture and sound.
"""

from pokegent.dashboard.stream.frames import HEIGHT, MAX_SCALE, WIDTH, Frame, FrameHub, rgb565

from .audio import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE, AudioHub

__all__ = [
    "AUDIO_CHANNELS",
    "AUDIO_SAMPLE_RATE",
    "AudioHub",
    "Frame",
    "FrameHub",
    "MAX_SCALE",
    "HEIGHT",
    "WIDTH",
    "rgb565",
]
