#!/usr/bin/env python3
"""Optional one-shot, idle-guarded progress review; never controls the game or owner lifecycle."""

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

PROMPT = (
    "Pokegent progress review: read fresh supported runtime status, the current objective and "
    "your private notes. Diagnose any legality pause immediately and set a fresh, checkable "
    "successor objective within minutes through the supported objective API with revision CAS. "
    "Continue authorized progress through the legal executor and retain every legality check. "
    "Record uncertain state in your notes. No further GO is needed. Do not send raw game input "
    "or control services. This idle-epoch review may arrive twice; treat it as one."
)
LIMIT = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url):
    deadline = time.monotonic() + 3
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=2) as response:
        if response.status != 200:
            raise ValueError("unavailable live media/telemetry")
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("live probe deadline")
            chunk = response.read1(65536)
            if not chunk:
                return response.headers, b"".join(chunks)
            total += len(chunk)
            if total > LIMIT:
                raise ValueError("oversized live response")
            chunks.append(chunk)


def number(value):
    return type(value) in {int, float} and math.isfinite(value)


def fresh(stamp, now, age):
    return number(stamp) and 0 <= now - stamp <= age


def media_available(dashboard, stream):
    """Supported operator telemetry + attached/capture health + bounded verified PNG.

    Equality may suppress a moving game during a sample race. Still images are allowed;
    missing evidence, old state and any recorded capture failure suppress delivery.
    """
    def document(path):
        headers, body = fetch(dashboard + path)
        if headers.get_content_type() != "application/json":
            raise ValueError("expected supported JSON telemetry")
        return json.loads(body)

    doc = document("/ops/telemetry")
    headers, body = fetch(stream + "/frame.png")
    if headers.get_content_type() != "image/png":
        return False
    with Image.open(io.BytesIO(body)) as picture:
        if picture.format != "PNG" or picture.size != (240, 160):
            return False
        picture.verify()
    health = document("/ops/health")
    now = time.time()
    state = doc["freshness"]["state"]
    frames, run = health["frames"], doc["run"]
    ttl = state["ttl_s"]
    valid = (
        doc["schema"] == "pokegent-viewer/1"
        and doc["server"]["emulator_ready"] is True
        and doc["state"]["valid"] is True
        and fresh(doc["generated_at"], now, 5)
        and number(ttl) and 0 < ttl <= 5 and fresh(state["updated_at"], now, ttl)
        and isinstance(run["run_id"], str) and bool(run["run_id"])
        and doc["game"]["run_id"] == run["run_id"] and run["status"] == "running"
        and health["ok"] is True and health["emulator_ready"] is True
        and health["status"] == "running" and frames["attached"] is True
        and type(frames["errors"]) is int and frames["errors"] == 0
        and frames["last_error"] is None
        and type(frames["seq"]) is int and frames["seq"] > 0
        and frames["seq"] == int(headers["X-Frame-Seq"])
        and type(run["emulator_frame"]) is int and run["emulator_frame"] >= 0
        and run["emulator_frame"] == frames["emulator_frame"] == int(headers["X-Emulator-Frame"])
    )
    return min(doc["generated_at"] + 5, state["updated_at"] + ttl) if valid else 0


def core(hc, *args):
    with tempfile.TemporaryFile() as output:
        subprocess.run([hc, *args], stdin=subprocess.DEVNULL, stdout=output,
                       stderr=subprocess.DEVNULL, timeout=5, check=True)
        output.seek(0)
        raw = output.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("oversized Core response")
        return json.loads(raw)


def epoch(value, now):
    """Installed Core Terminal schema: status_ms is ms since epoch, not a screen heuristic."""
    if (value["state"] != "live" or value["status"]["state"] != "idle"
            or value["status"].get("app") != "hermes"):
        return None
    created, idle, pid = value["created_ms"], value["status_ms"], value["pid"]
    foreground = value["foreground"]["pid"]
    if (any(type(x) is not int or x <= 0 for x in (created, idle, pid, foreground))
            or not created <= idle < now * 1000 - 300000):
        return None
    return [value["address"], created, pid, foreground, idle]


def private_directory(directory):
    if directory.resolve() != directory:
        raise ValueError("heartbeat state requires an absolute canonical directory")
    if any((parent / ".git").is_file() or (parent / ".git/HEAD").is_file()
           for parent in (directory, *directory.parents)):
        raise ValueError("heartbeat state must be outside Git checkouts")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = directory.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("heartbeat state requires a canonical private owner directory")


def private_open(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    import stat
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise ValueError("unsafe heartbeat state file")
    return os.fdopen(fd, "r+")


def commit_receipt(directory, key):
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(key, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / "receipt.json")
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def heartbeat(directory, target, dashboard, stream, hc="hc", *, query=core, media=media_available):
    private_directory(directory)
    with private_open(directory / "heartbeat.lock") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        original = epoch(query(hc, "get", "--json", target), time.time())
        if original is None:
            return False
        key = [target, *original]
        receipt = directory / "receipt.json"
        if receipt.exists() or receipt.is_symlink():
            with private_open(receipt) as source:
                raw = source.read(4097)
                if len(raw) > 4096:
                    raise ValueError("invalid receipt")
                if json.loads(raw) == key:
                    return False
        valid_until = media(dashboard, stream)
        if not number(valid_until) or valid_until <= time.time():
            return False
        if epoch(query(hc, "get", "--json", target), time.time()) != original:
            return False
        if time.time() >= valid_until:
            return False
        review_id = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        # The final operation atomically guards idle. Never use force or an unguarded transport.
        accepted = query(hc, "send", "--json", "--if", "idle", target,
                         PROMPT + " Review id: " + review_id)
        if (accepted.get("status") != "idle" or type(accepted.get("turn")) is not int
                or accepted["turn"] < 0):
            return False
        commit_receipt(directory, key)  # accepted-send/write failure is explicitly at least once
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--dashboard", required=True, help="operator dashboard origin")
    parser.add_argument("--stream", required=True, help="picture stream origin")
    parser.add_argument("--hc", default="hc")
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+/)?t[0-9a-z]+", args.target):
        parser.error("use a stable Core terminal address, not a mutable name")
    for url in (args.dashboard, args.stream):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path):
            parser.error("media origins require http(s) without credentials, path or query")
    try:
        heartbeat(args.state, args.target, args.dashboard, args.stream, args.hc)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        print("owner heartbeat suppressed: unavailable or unsupported evidence/state",
              file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
