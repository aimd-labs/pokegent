#!/usr/bin/env python3
"""Check offline raw mGBA RAM images: --states DIR [--pattern '*.state'] [--limit N].

Source-checkout script; stdout JSON only. Exit 0: all conform; 1: RAM failures;
2: input/profile/usage error. Zero limit means unlimited; no files are written.
Adapted from tools/ram_conformance.py, source A at
7c8742ba50a7a482e24d69bb1364c49e63c4e453. MIT; see LICENSE and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokegent.games.gen3.firered.rom.conformance import (  # noqa: E402
    StateFormatError,
    run_state_dir,
)
from pokegent.games.gen3.firered.rom.rom_data import ProfileError, load_profiles  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--states", type=Path, required=True)
    parser.add_argument("--pattern", default="*.state")
    parser.add_argument("--limit", type=int, default=0, help="maximum states; 0 means unlimited")
    parser.add_argument("--profile", default="firered-us-rev0")
    parser.add_argument("--quiet", action="store_true", help="retain totals and failed entries")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    try:
        profile = load_profiles().get(args.profile)
        if not profile.supported:
            raise ProfileError("unsupported profile")
        if not args.states.is_dir():
            raise OSError("--states must be a readable directory")
        # The library opens matches directly; reject directories/FIFOs before any read.
        paths = sorted(args.states.glob(args.pattern))
        if args.limit > 0:
            paths = paths[:args.limit]
        if any(not path.is_file() for path in paths):
            raise OSError("matched state must be a readable file")
        report = run_state_dir(args.states, profile, pattern=args.pattern, limit=args.limit)
        if not report["tested"]:
            raise ValueError("no states tested")
    except (OSError, StateFormatError, ProfileError, ValueError, KeyError,
            TypeError, AttributeError, NotImplementedError) as exc:
        message = " ".join(str(exc).split())[:400]
        print(f"error: {message}", file=sys.stderr)
        return 2
    if args.quiet:
        report = {**report, "results": [result for result in report["results"] if not result["ok"]]}
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
