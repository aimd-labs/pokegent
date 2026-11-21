"""Read-only run ledger CLI commands."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def _path(args) -> Path:
    if args.db:
        return Path(args.db).expanduser()
    from pokegent.cli import live_home

    return live_home(args.live_home) / "ledger.sqlite3"


def _json_value(value):
    if isinstance(value, bytes):
        return {"type": "blob", "hex": value.hex()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _text(args, result) -> str:
    if args.ledger_action == "sql":
        return json.dumps(_json_value(result), indent=2, ensure_ascii=True, allow_nan=False)
    if args.ledger_action == "incidents":
        title = f"Incidents for save {args.save}: {len(result)}"
    elif args.ledger_action == "milestones":
        title = f"Milestones for save {args.save}: {len(result)}"
    else:
        save = result["save"]
        title = (
            f"Save {save['id']} ({save['label'] or 'unlabeled'}): "
            f"{len(result['runs'])} runs, {len(result['objectives'])} objectives, "
            f"{len(result['milestones'])} historical milestone observations"
        )
    return title + "\n" + json.dumps(
        _json_value(result), indent=2, ensure_ascii=True, allow_nan=False
    )


def run(args) -> int:
    from pokegent.ledger_queries import incidents, milestones, open_readonly, query, status

    try:
        with closing(open_readonly(_path(args))) as db:
            if args.ledger_action == "sql":
                result = query(db, args.sql)
            else:
                if not query(db, "SELECT 1 FROM saves WHERE id=?", (args.save,)):
                    raise KeyError(args.save)
                if args.ledger_action == "incidents":
                    result = incidents(db, args.save)
                elif args.ledger_action == "milestones":
                    result = milestones(db, args.save)
                else:
                    result = status(db, args.save)
        if args.json:
            print(json.dumps(
                _json_value(result), indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False
            ))
        else:
            print(_text(args, result))
        return 0
    except KeyError:
        print(f"ERROR: unknown save: {args.save}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"ERROR: ledger database not found: {_path(args)}", file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
