"""SYSTEM1 HTTP client and provider (spec 8.1, 8.4, 12.3). Own implementation of the documented API.

Wire protocol (wfzyx/von README, pinned in ``source-lock.md``): ``POST {base}/v1/systemone`` with
``{"state", "questions": {name: {"type": "choice", "instructions", "criteria"}}}``.
An explicit ``config.request_model`` adds JSON ``model``; unset means omitted. The pinned
historical upstream rejected that field; other endpoints may require it. ``config.model``
is only the expected response id, never a request selector. Answer ``{"model", "answers": {name:
{"type": "choice", "choice", "probabilities", "confidence"}}, "usage": {"input_tokens",
"output_tokens"}, "truncation"?}``, where ``model`` is a string or an object holding ``model``.
Optional bearer auth
(``Authorization: Bearer <key>``). A middle-truncated state is reported by the upstream
``X-Von-Truncated``
and ``Warning`` headers and a ``truncation`` field; ``--on-overflow refuse`` answers HTTP 422.

Resilience, all parametrized by :mod:`policy`:

* connect/read timeouts, both capped by the remaining decision deadline;
* bounded retries with exponential backoff and jitter for transient errors only (timeout, 408,
  429, 5xx, connection errors); a 4xx is final and never retried; ``Retry-After`` is honored
  within the deadline;
* a circuit breaker (closed / open / half-open) over transient failures;
* a token-bucket budget over HTTP attempts (protects the shared lanes);
* response validation: the answer must be one of the criteria keys sent; probabilities finite,
  inside [0, 1], keys within the criteria, summing to about 1; confidence finite in [0, 1].

No secret is logged: the logger carries a redacting filter, URLs are stripped of userinfo/query,
and error text is scrubbed against the configured key.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import math
import random
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from pokegent.errors import ErrorCode

from .policy import BreakerPolicy, RatePolicy, RetryPolicy, SystemOneConfig
from .provider import (
    CircuitOpen,
    InvalidResponse,
    ProviderChoice,
    ProviderConfigurationError,
    ProviderHealth,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    RateBudgetExceeded,
)
from .questions import PROMPT_SCHEMA_VERSION
from .redact import RedactingFilter, Scrubber, safe_url

CLIENT_VERSION = "system1-http/1"
ENDPOINT = "/v1/systemone"
MAX_RESPONSE_BYTES = 1_048_576
CONFIGURATION_STATUSES = frozenset({400, 401, 403, 404, 405, 415, 422})
# A resolver in a thread cannot be interrupted or safely abandoned at interpreter exit.
_RESOLVE_CODE = (
    "import json,socket,sys; "
    "print(json.dumps(socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),"
    "type=socket.SOCK_STREAM)))"
)
UPSTREAM_SHA = "56e845299f573af06480db6035f5d13f0f3baf51"  # wfzyx/von, see source-lock.md

# Wire names of the pinned upstream server. Neutral aliases are accepted for test doubles and
# future deployments, but the upstream names must remain supported for the real server.
UPSTREAM_TRUNCATED_HEADER = "x-von-truncated"
NEUTRAL_TRUNCATED_HEADER = "x-system1-truncated"
UPSTREAM_VERSION_FIELDS = ("von_version", "x-von-version")
NEUTRAL_VERSION_FIELDS = ("system1_version", "x-system1-version")

log = logging.getLogger("pokegent.system_one")
#: Every configured key is registered here; the filter runs on every record of ``log``.
LOG_SCRUBBER = Scrubber()
log.addFilter(RedactingFilter(LOG_SCRUBBER))


# -- circuit breaker ----------------------------------------------------------------------------


class CircuitBreaker:
    """closed -> open after ``failure_threshold`` consecutive transient failures; open -> half-open
    after ``open_seconds``; half-open admits ``half_open_max_calls`` probes: one success closes,
    one failure re-opens. Thread-safe; the clock is injectable."""

    def __init__(
        self, policy: BreakerPolicy | None = None, *, clock: Callable[[], float] = time.monotonic
    ):
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probes = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._peek()

    def _peek(self) -> str:
        if self._state == "open" and self._clock() - self._opened_at >= self.policy.open_seconds:
            self._state, self._probes = "half_open", 0
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def before_call(self) -> None:
        with self._lock:
            state = self._peek()
            if state == "open":
                wait = self.policy.open_seconds - (self._clock() - self._opened_at)
                raise CircuitOpen(f"circuit open, next probe in {max(wait, 0):.1f}s")
            if state == "half_open":
                if self._probes >= self.policy.half_open_max_calls:
                    raise CircuitOpen("circuit half-open, probe already in flight")
                self._probes += 1

    def release_probe(self) -> None:
        with self._lock:
            self._probes = max(0, self._probes - 1)

    def record_success(self) -> None:
        with self._lock:
            self._state, self._failures, self._probes = "closed", 0, 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "half_open" or self._failures >= self.policy.failure_threshold:
                self._state, self._opened_at, self._probes = "open", self._clock(), 0


# -- request budget -----------------------------------------------------------------------------


class RequestBudget:
    """Token bucket: ``max_requests`` per ``per_seconds``, bucket size ``burst``."""

    def __init__(
        self, policy: RatePolicy | None = None, *, clock: Callable[[], float] = time.monotonic
    ):
        self.policy = policy or RatePolicy()
        self._clock = clock
        self._lock = threading.Lock()
        self._capacity = float(self.policy.burst or self.policy.max_requests)
        self._tokens = self._capacity
        self._rate = self.policy.max_requests / self.policy.per_seconds
        self._stamp = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def wait_needed(self) -> float:
        with self._lock:
            self._refill()
            return max(0.0, (1.0 - self._tokens) / self._rate)


# -- stats --------------------------------------------------------------------------------------


def percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(q * len(sorted_values)))
    return float(sorted_values[rank - 1])


class ProviderStats:
    def __init__(self, window: int = 500) -> None:
        self._lock = threading.Lock()
        self._lat: deque[float] = deque(maxlen=window)
        self.requests = self.failures = self.invalid = self.truncated = 0
        self.last_error: str | None = None

    def ok(self, latency_ms: float, *, truncated: bool) -> None:
        with self._lock:
            self.requests += 1
            self.truncated += int(truncated)
            self._lat.append(latency_ms)

    def failure(self, message: str, *, invalid: bool = False) -> None:
        with self._lock:
            self.requests += 1
            self.failures += int(not invalid)
            self.invalid += int(invalid)
            self.last_error = message

    def latency(self) -> dict[str, float | None]:
        with self._lock:
            ordered = sorted(self._lat)
        return {f"p{int(q * 100)}": percentile(ordered, q) for q in (0.5, 0.95, 0.99)}


# -- response validation ------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise InvalidResponse(f"non-finite JSON number {name} in response")


def _short(value: Any, n: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= n else text[: n - 3] + "..."


def _published_text(value: str | None, scrubber: Scrubber, n: int = 200) -> str | None:
    if value is None:
        return None
    safe = scrubber.text(value)
    safe = " ".join("".join(c if c.isprintable() else " " for c in safe).split())
    return scrubber.text(safe)[:n]


def _cap_json(value: Any, n: int = 2000) -> Any:
    """Server-provided metadata ends up in the calibration log: bound its size."""
    if value is None:
        return None
    text = json.dumps(value, default=str, sort_keys=True)
    return value if len(text) <= n else {"truncated_metadata": text[:n]}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class ParsedChoice:
    choice: str
    probabilities: dict[str, float]
    confidence: float | None
    response_model: str | None
    usage_in: int | None
    usage_out: int | None
    truncation: Any
    argmax_mismatch: bool


def _response_model(value: Any) -> str | None:
    """The model id of an answer: a string, or an object whose ``model`` is the string."""
    if isinstance(value, dict):
        value = value.get("model")
    return value if isinstance(value, str) and value else None


def parse_choice_response(
    body: bytes | str,
    *,
    question_name: str,
    criteria_keys: Sequence[str],
    prob_sum_tolerance: float = 0.02,
) -> ParsedChoice:
    """Validate a SYSTEM1 answer for one Choice question. Raises :class:`InvalidResponse`."""
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except InvalidResponse:
        raise
    except ValueError:
        raise InvalidResponse("response is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise InvalidResponse("response has no 'answers' object")
    answer = data["answers"].get(question_name)
    if not isinstance(answer, dict):
        raise InvalidResponse(f"response has no answer for question {question_name!r}")
    choice = answer.get("choice")
    allowed = set(criteria_keys)
    if not isinstance(choice, str) or choice not in allowed:
        raise InvalidResponse(
            f"answer {_short(choice)} is not one of the {len(allowed)} criteria sent",
            code=ErrorCode.E_CHOICE_NOT_IN_FRAME,
        )
    probs: dict[str, float] = {}
    raw_probs = answer.get("probabilities")
    if raw_probs is not None:
        if not isinstance(raw_probs, dict):
            raise InvalidResponse("probabilities must be an object")
        for key, value in raw_probs.items():
            p = _number(value)
            if p is None or not math.isfinite(p) or not (-1e-9 <= p <= 1 + 1e-9):
                raise InvalidResponse(
                    f"probability for {_short(key)} is not a finite number in [0, 1]"
                )
            if key not in allowed:
                raise InvalidResponse(f"probabilities name an unknown key {_short(key)}")
            probs[key] = min(1.0, max(0.0, p))
        if probs:
            if abs(sum(probs.values()) - 1.0) > prob_sum_tolerance:
                raise InvalidResponse(f"probabilities sum to {sum(probs.values()):.4f}, not 1")
            if choice not in probs:
                raise InvalidResponse("the chosen key has no probability")
    confidence: float | None = None
    if answer.get("confidence") is not None:
        confidence = _number(answer["confidence"])
        if confidence is None or not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
            raise InvalidResponse("confidence is not a finite number in [0, 1]")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    model = _response_model(data.get("model"))
    mismatch = bool(probs) and probs[choice] < max(probs.values()) - 1e-9
    return ParsedChoice(
        choice=choice,
        probabilities=probs,
        confidence=confidence,
        response_model=model,
        usage_in=usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None,
        usage_out=usage.get("output_tokens")
        if isinstance(usage.get("output_tokens"), int)
        else None,
        truncation=data.get("truncation"),
        argmax_mismatch=mismatch,
    )


def response_ref(parsed: ParsedChoice) -> str:
    """Deterministic reference to the normalized answer (content hash, not a file path)."""
    canon = json.dumps(
        {"c": parsed.choice, "p": parsed.probabilities, "q": parsed.confidence},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "http:sha256:" + hashlib.sha256(canon.encode()).hexdigest()[:24]


def rejection_reason(body: bytes, scrubber: Scrubber) -> str:
    """Only recognized JSON error fields; scrub before bounding, never dump raw bodies."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return "no recognized JSON reason"

    def extract(value, depth=0):
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and depth < 3:
            for key in ("message", "detail", "error"):
                text = extract(value.get(key), depth + 1)
                if text:
                    return text
        return None

    reason = extract(value) if isinstance(value, dict) else None
    safe = scrubber.text(reason or "no recognized JSON reason")
    normalized = " ".join("".join(c if c.isprintable() else " " for c in safe).split())
    return scrubber.text(normalized)[:240]


# -- transport ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int

    @property
    def truncated_header(self) -> bool:
        return any(
            (value := self.headers.get(name)) is not None
            and value.strip().lower() not in ("", "0", "false", "no")
            for name in (UPSTREAM_TRUNCATED_HEADER, NEUTRAL_TRUNCATED_HEADER)
        )


@dataclass
class VonClient:
    """One endpoint, with timeouts, retries, breaker and budget. Blocking; run it in a thread."""

    base_url: str
    api_key: str | None = field(default=None, repr=False)
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 8.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    budget: RequestBudget = field(default_factory=RequestBudget)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random
    scrubber: Scrubber = field(default_factory=Scrubber)
    endpoint_path: str = ENDPOINT
    auth_header: str = "Authorization"

    def __post_init__(self) -> None:
        self._active_lock = threading.Lock()
        self._active: dict[http.client.HTTPConnection, Callable[[], None]] = {}
        self._cancelled = threading.Event()
        self.scrubber.add(self.api_key)
        LOG_SCRUBBER.add(self.api_key)
        parts = urlsplit(self.base_url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        path = parts.path.rstrip("/")
        endpoint = self.endpoint_path.rstrip("/") or "/"
        # Preserve base prefixes, and overlap e.g. /api/v1 with /v1/chat/completions.
        prefix, suffix = path.split("/"), endpoint.split("/")[1:]
        overlap = 0
        for count in range(1, min(len(prefix), len(suffix)) + 1):
            if prefix[-count:] == suffix[:count]:
                overlap = count
        self._path = path + ("/" + "/".join(suffix[overlap:]) if suffix[overlap:] else "")
        self._health_path = self._path.removesuffix(endpoint) + "/health"
        self.url_for_log = safe_url(self.base_url).removesuffix(path) + self._path


    def __repr__(self) -> str:
        return f"VonClient(url={self.url_for_log!r}, auth={'on' if self.api_key else 'off'})"

    # -- one HTTP attempt ---------------------------------------------------------------------

    def _resolve(self, deadline, expired):
        port = self._port or (443 if self._https else 80)
        # Numeric addresses need no resolver process (including ordinary loopback tests).
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                socket.inet_pton(family, self._host)
            except OSError:
                continue
            address = ((self._host, port) if family == socket.AF_INET
                       else (self._host, port, 0, 0))
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]
        with subprocess.Popen(
            [sys.executable, "-I", "-c", _RESOLVE_CODE, self._host, str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as resolver:
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or expired.is_set() or self._cancelled.is_set():
                        raise TimeoutError("resolver deadline expired or cancelled")
                    try:
                        output, _ = resolver.communicate(timeout=min(remaining, .02))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if resolver.returncode:
                    raise socket.gaierror("name resolution failed")
                return json.loads(output)
            finally:
                if resolver.poll() is None:
                    resolver.kill()
                resolver.wait()

    def _exchange(self, method, path, body, headers, remaining):
        if self._cancelled.is_set():
            raise ConnectionAbortedError("request cancelled")
        connect_t = min(self.connect_timeout_s, remaining)
        deadline = time.monotonic() + remaining
        read_t = min(self.read_timeout_s, remaining)
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=connect_t)
        expired = threading.Event()
        sockets = []

        def expire():
            expired.set()
            # HTTPConnection can detach its socket for a Connection: close response.
            # Retain it so shutdown interrupts the response file's blocking reads too.
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Only the reader closes HTTPResponse/HTTPConnection. Closing here races
            # HTTPResponse._close_conn during chunked reads on Python 3.11.
            self._abort_connection(conn)

        def connect(address, timeout, source_address=None):
            connect_deadline = min(deadline, time.monotonic() + connect_t)
            addresses = self._resolve(connect_deadline, expired)
            last = None
            for family, kind, proto, _, target in addresses:
                left = connect_deadline - time.monotonic()
                if left <= 0 or expired.is_set() or self._cancelled.is_set():
                    raise TimeoutError("connect deadline expired or cancelled")
                sock = socket.socket(family, kind, proto)
                sockets.append(sock)
                try:
                    sock.settimeout(left)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(tuple(target))
                    # TLS opening must use only the budget left after DNS/TCP opening.
                    sock.settimeout(max(.001, connect_deadline - time.monotonic()))
                    return sock
                except OSError as exc:
                    last = exc
                    sock.close()
            raise last or OSError("resolver returned no addresses")

        conn._create_connection = connect

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        with self._active_lock:
            self._active[conn] = expire
        timer.start()
        try:
            if self._cancelled.is_set():
                raise ConnectionAbortedError("request cancelled")
            conn.connect()
            assert conn.sock is not None
            sockets.append(conn.sock)
            if expired.is_set() or self._cancelled.is_set():
                raise TimeoutError("request deadline expired or cancelled")
            conn.sock.settimeout(read_t)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            try:
                terminal = resp.status in CONFIGURATION_STATUSES
                if terminal:
                    # A reason is optional: leave time to publish the authoritative status
                    # before the caller's decision deadline, even for a dribbling error body.
                    timer.cancel()
                    timer = threading.Timer(
                        max(0, min(read_t, (deadline - time.monotonic()) / 2)), expire
                    )
                    timer.daemon = True
                    timer.start()
                try:
                    data = resp.read(MAX_RESPONSE_BYTES + 1)
                except (OSError, http.client.HTTPException):
                    if not terminal:
                        raise
                    data = b""  # The HTTP rejection remains authoritative without a reason.
                if expired.is_set() and not terminal:
                    raise TimeoutError("request deadline expired")
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
            finally:
                resp.close()
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise TimeoutError("request deadline expired") from None
            raise
        finally:
            timer.cancel()
            with self._active_lock:
                self._active.pop(conn, None)
            conn.close()

    def _post(self, body: bytes, headers: dict[str, str], remaining: float):
        return self._exchange("POST", self._path, body, headers, remaining)

    @staticmethod
    def _abort_connection(conn: http.client.HTTPConnection) -> None:
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self) -> None:
        """Abort active reads and make a pre-connect race fail immediately after connect."""
        self._cancelled.set()
        self.abort_active()

    def abort_active(self) -> None:
        with self._active_lock:
            active = tuple(self._active.values())
        for abort in active:
            abort()

    def fetch_health(self, *, timeout_s: float = 3.0) -> dict[str, Any] | None:
        """``GET {base}/health``: server and weights revision, no inference, no credential sent.

        Informational only (``provider check --live``): any failure or odd shape gives ``None``.
        """
        try:
            status, _, data = self._exchange(
                "GET", self._health_path, None, {"Accept": "application/json"}, timeout_s
            )
            if status != 200 or len(data) > MAX_RESPONSE_BYTES:
                return None
            parsed = json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            return None
        if not isinstance(parsed, dict):
            return None
        parsed = self.scrubber.value(parsed)
        version = next(
            (
                parsed.get(k)
                for k in (*UPSTREAM_VERSION_FIELDS, *NEUTRAL_VERSION_FIELDS)
                if parsed.get(k)
            ),
            None,
        )
        result = ({"system1_version": _published_text(str(version), self.scrubber, 80)}
                  if version else {})
        result.update(
            {
                k: _published_text(str(parsed[k]), self.scrubber, 80)
                for k in ("weights_revision", "status")
                if parsed.get(k) is not None
            }
        )
        return result

    # -- bounded retries ----------------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        p = self.retry
        delay = min(p.backoff_max_seconds, p.backoff_base_seconds * (2 ** (attempt - 1)))
        delay = delay * (1.0 - p.jitter + p.jitter * self.rng())
        if retry_after is not None:
            delay = max(delay, min(retry_after, p.backoff_max_seconds))
        return delay

    def request(
        self, payload: Mapping[str, Any], *, request_id: str, deadline: float
    ) -> RawResponse:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            if self._cancelled.is_set():
                raise ProviderTimeout("request cancelled", attempt=attempt)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise last or ProviderTimeout("decision deadline used up before the request")
            self.breaker.before_call()
            if not self.budget.try_acquire():
                self.breaker.release_probe()  # nothing was sent: no evidence either way
                raise RateBudgetExceeded(
                    "local request budget used up", wait_s=self.budget.wait_needed()
                )
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"pokegent/{CLIENT_VERSION}",
                "X-Request-Id": f"{request_id}.a{attempt}",
            }
            if self.api_key:
                headers[self.auth_header] = (f"Bearer {self.api_key}"
                                             if self.auth_header.lower() == "authorization"
                                             else self.api_key)
            log.debug("POST %s request_id=%s attempt=%d", self.url_for_log, request_id, attempt)
            retry_after: float | None = None
            try:
                status, hdrs, data = self._post(body, headers, remaining)
            except TimeoutError:
                last = ProviderTimeout("request timed out", attempt=attempt)
            except (OSError, http.client.HTTPException) as exc:
                if self._cancelled.is_set():
                    raise ProviderTimeout("request cancelled", attempt=attempt) from None
                last = ProviderUnavailable(
                    self.scrubber.text(f"connection failed: {type(exc).__name__}"), attempt=attempt
                )
            else:
                if 200 <= status < 300:
                    self.breaker.record_success()
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise InvalidResponse("response body too large")
                    return RawResponse(status, hdrs, data, attempt)
                if status == 408:
                    last = ProviderTimeout("server reported request timeout", status=status)
                elif status == 429 or status >= 500:
                    ra = hdrs.get("retry-after", "")
                    retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None
                    last = ProviderUnavailable(f"server answered HTTP {status}", status=status)
                else:  # 4xx: the request is wrong; the server is fine; never retry
                    self.breaker.record_success()
                    reason = (rejection_reason(data, self.scrubber)
                              if len(data) <= MAX_RESPONSE_BYTES
                              else self.scrubber.text(
                                  "response body too large; explanation discarded"))
                    hint = ("check credentials/auth_header" if status in {401, 403}
                            else "check request_model, protocol and endpoint_path")
                    error = (ProviderConfigurationError
                             if status in CONFIGURATION_STATUSES
                             else ProviderRejected)
                    raise error(
                        self.scrubber.text(f"server rejected HTTP {status}: {reason}; {hint}"),
                        status=status, reason=reason,
                    )

            self.breaker.record_failure()
            if attempt >= self.retry.max_attempts:
                break
            delay = self._backoff(attempt, retry_after)
            if delay >= deadline - self.clock():
                break  # sleeping would eat the whole budget; fail now and let the runner decide
            log.warning(
                "System One attempt %d failed (%s); retrying in %.2fs",
                attempt,
                type(last).__name__,
                delay,
            )
            if self.sleep is time.sleep:
                if self._cancelled.wait(delay):
                    raise ProviderTimeout("request cancelled", attempt=attempt)
            else:
                self.sleep(delay)
        assert last is not None
        raise last


# -- provider -----------------------------------------------------------------------------------


class HttpProvider:
    """``SystemOneProvider`` for a SYSTEM1 (or TypeSafe-compatible) ``/v1/systemone`` server."""

    provider_id = "http"
    provider_version = CLIENT_VERSION

    def __init__(
        self,
        config: SystemOneConfig,
        *,
        client: VonClient | None = None,
        scrubber: Scrubber | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not config.provider_url:
            raise ValueError("HttpProvider needs provider_url")
        self.config = config
        self.model_id = config.model or ""  # the EXPECTED id; never sent (see the module docstring)
        self.scrubber = scrubber or Scrubber()
        self.scrubber.add(config.api_key_value())
        LOG_SCRUBBER.add(config.api_key_value())
        self._clock = clock
        self.client = client or VonClient(
            base_url=config.provider_url,
            api_key=config.api_key_value(),
            connect_timeout_s=config.connect_timeout_seconds,
            read_timeout_s=config.effective_read_timeout(),
            retry=config.retry,
            breaker=CircuitBreaker(config.breaker, clock=clock),
            budget=RequestBudget(config.rate, clock=clock),
            clock=clock,
            endpoint_path=config.endpoint_path,
            auth_header=config.auth_header,
            scrubber=self.scrubber,
        )
        self.stats = ProviderStats()
        self.server_version: str | None = None
        self.response_model: str | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"HttpProvider(url={self.client.url_for_log!r}, model={self.model_id!r})"

    def _executor(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="system1-http")
            return self._pool

    def _payload(self, state, question, name):
        wire_q = {k: question[k] for k in ("type", "instructions", "criteria")}
        payload = {"state": state, "questions": {name: wire_q}}
        if self.config.request_model:
            payload["model"] = self.config.request_model
        return payload

    def _parse(self, body, name, criteria):
        return parse_choice_response(body, question_name=name, criteria_keys=criteria)

    # -- SystemOneProvider --------------------------------------------------------------------

    async def decide(
        self, *, state: dict, question: dict, request_id: str, timeout_s: float
    ) -> ProviderChoice:
        name = str(question.get("name") or "action")
        payload = self._payload(state, question, name)
        t0 = self._clock()
        deadline = t0 + timeout_s
        loop = asyncio.get_running_loop()
        try:
            raw = await asyncio.wait_for(loop.run_in_executor(
                self._executor(),
                partial(self.client.request, payload, request_id=request_id, deadline=deadline),
            ), timeout=timeout_s)
            parsed = self._parse(raw.body, name, list(question["criteria"]))
            if self.config.strict_model_pin and parsed.response_model != self.model_id:
                raise ProviderConfigurationError(
                    "reported model is missing or differs from pinned model; "
                    "check the actual response identity and configured pin",
                    reason="response_model_pin",
                )
        except asyncio.CancelledError:
            self.client.abort_active()
            raise
        except TimeoutError:
            # Abort these attempts without poisoning future outage recovery decisions.
            self.client.abort_active()
            raise ProviderTimeout("decision deadline used up") from None
        except InvalidResponse as exc:
            safe = self.scrubber.text(str(exc))
            self.stats.failure(safe, invalid=True)
            raise InvalidResponse(safe, code=exc.code) from None
        except Exception as exc:
            self.stats.failure(self.scrubber.text(str(exc)))
            raise
        latency_ms = (self._clock() - t0) * 1000.0
        truncated = raw.truncated_header or bool(parsed.truncation)
        self.stats.ok(latency_ms, truncated=truncated)
        self.response_model = _published_text(parsed.response_model, self.scrubber)
        self.server_version = _published_text(
            next(
                (
                    raw.headers.get(k)
                    for k in (*UPSTREAM_VERSION_FIELDS, *NEUTRAL_VERSION_FIELDS)
                    if raw.headers.get(k)
                ),
                None,
            )
            or raw.headers.get("server")
            or None,
            self.scrubber, 80,
        )
        return ProviderChoice(
            option_id=parsed.choice,
            probabilities=parsed.probabilities,
            confidence=parsed.confidence,
            model_id=self.response_model or "",
            provider_id=self.provider_id,
            latency_ms=latency_ms,
            truncated=truncated,
            raw_response_ref=response_ref(parsed),
            meta={
                "attempts": raw.attempts,
                "usage_in": parsed.usage_in,
                "usage_out": parsed.usage_out,
                "truncation": _cap_json(self.scrubber.value(parsed.truncation)),
                "truncation_warning": _published_text(raw.headers.get("warning"), self.scrubber),
                "response_model": self.response_model,
                "model_mismatch": bool(
                    self.model_id
                    and parsed.response_model
                    and parsed.response_model != self.model_id
                ),
                "argmax_mismatch": parsed.argmax_mismatch,
                "server_version": self.server_version,
                "breaker": self.client.breaker.state,
            },
        )

    async def health(self) -> ProviderHealth:
        lat = self.stats.latency()
        state = self.client.breaker.state
        return ProviderHealth(
            ok=state != "open",
            state=state,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            model_id=_published_text(self.model_id, self.scrubber) or "",
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
            requests=self.stats.requests,
            failures=self.stats.failures,
            invalid=self.stats.invalid,
            truncated=self.stats.truncated,
            consecutive_failures=self.client.breaker.consecutive_failures,
            latency_p50_ms=lat["p50"],
            latency_p95_ms=lat["p95"],
            latency_p99_ms=lat["p99"],
            last_error=self.stats.last_error,
        )

    def cancel(self) -> None:
        self.client.cancel()

    async def probe(self) -> ProviderChoice:
        """ONE tiny Choice request (used by ``provider check --live`` only)."""
        question = {
            "name": "action",
            "type": "choice",
            "instructions": "Pick the option that is a color.",
            "criteria": {"red": "A color.", "run": "A verb."},
        }
        return await self.decide(
            state={"check": "pokegent provider check"},
            question=question,
            request_id="provider-check",
            timeout_s=self.config.timeout_seconds,
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "server_version": _published_text(self.server_version, self.scrubber, 80),
            "model_id": _published_text(self.model_id, self.scrubber),
            "response_model": _published_text(self.response_model, self.scrubber),
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "upstream_sha": UPSTREAM_SHA,
        }

    def close(self) -> None:
        self.cancel()
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
