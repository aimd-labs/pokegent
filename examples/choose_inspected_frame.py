"""Inspect one agent frame and submit a choice bound to that exact frame."""

import argparse
import json

from pokegent.server import request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19760)
    args = parser.parse_args()
    if not 19700 <= args.port <= 19799:
        parser.error("port must be within 19700-19799")
    print(json.dumps(request(args.port, "/briefing"), indent=2))
    frame = request(args.port, "/frame?wait=0")
    print(json.dumps(frame, indent=2))
    if "frame_id" not in frame:
        return  # No pending decision; run again when /status reports waiting_for_choice.
    option_id = input("Choose an offered option id (empty to leave pending): ").strip()
    if not option_id:
        return
    if option_id not in {option["id"] for option in frame["options"]}:
        raise ValueError("option_id was not offered in the inspected frame")
    result = request(args.port, "/choose", body={
        "frame_id": frame["frame_id"], "option_id": option_id,
    })
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
