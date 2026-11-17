#!/usr/bin/env python3
"""Compatibility entry point for supervisor/observer run-end classification."""

from pokegent.live import main, payload, verdict  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
