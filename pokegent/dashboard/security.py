"""Host validation shared by the loopback-only dashboard and media server."""
from __future__ import annotations

import re
from collections.abc import Iterable

_LOOPBACK_AUTHORITY = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?", re.I)


def loopback_host_allowed(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    values = [value for name, value in headers if name.lower() == b"host"]
    if len(values) != 1:
        return False
    try:
        match = _LOOPBACK_AUTHORITY.fullmatch(values[0].decode("ascii"))
    except UnicodeDecodeError:
        return False
    return match is not None and (match[1] is None or 1 <= int(match[1]) <= 65535)
