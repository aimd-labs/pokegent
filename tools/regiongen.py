#!/usr/bin/env python3
"""CLI compatibility wrapper for the packaged ROM region generator."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokegent.games.gen3.firered.rom.regiongen import generate, main  # noqa: E402

__all__ = ["generate", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
