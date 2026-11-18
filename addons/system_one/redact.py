"""Secret scrubbing for logs, events, errors and the calibration file (spec 8.4, 0.2 rule 12).

Two layers: exact known secrets (the configured API key, however odd its shape) and
token-looking patterns (``Bearer ...``, ``sk-...``). Keys are never masked by name here: the
calibration file legitimately carries fields such as ``usage_in`` and must stay readable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pokegent.events import REDACTED, default_redact

_PATTERN = re.compile(
    r"(\bBearer\s+[A-Za-z0-9._\-~+/=]{6,}|\b(sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_\-]{12,})"
)


class Scrubber:
    """Replaces known secrets and token-looking strings in text and JSON-like values."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for s in secrets:
            self.add(s)

    def add(self, secret: str | None) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)  # longest first: no partial leftovers

    def __repr__(self) -> str:  # never show what it protects
        return f"Scrubber(n={len(self._secrets)})"

    def text(self, value: str) -> str:
        if self._secrets:
            value = re.sub("|".join(re.escape(s) for s in self._secrets),
                           lambda _: REDACTED, value)
        return _PATTERN.sub(REDACTED, value)

    def value(self, value: Any) -> Any:
        """Recursive scrub of a JSON-like value (strings and mapping keys)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(k)): self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value

    def event_details(self, details: Mapping[str, Any]) -> dict[str, Any]:
        """Scrub, then apply the event log's own key-based masking (same guarantees as events)."""
        return default_redact(self.value(dict(details)))


class RedactingFilter(logging.Filter):
    """Logging filter: no record leaves the logger with a secret in message or arguments."""

    def __init__(self, scrubber: Scrubber) -> None:
        super().__init__()
        self._scrubber = scrubber

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrubber.text(record.getMessage())
        record.args = ()
        return True


def safe_url(url: str) -> str:
    """URL without userinfo and query string (either may carry a credential)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))
