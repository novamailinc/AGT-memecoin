#!/usr/bin/env python3
"""Check the paper bot's tuning-proposal store for NEW open entry-learner
proposals (area == "entries"). Prints JSON and records reported IDs in the
marker file so each proposal is reported exactly once.

Exit 0 always; output: {"new": [...], "open_entry_count": N}
"""
import json
import os
import sys

REPO = os.path.expanduser("~/workspace/Forex-agent/Forex-agent-main")
VENV_PY = os.path.expanduser("~/workspace/Forex-agent/.venv/bin/python")
MARKER = os.path.expanduser("~/workspace/entry-learner-watch/reported.json")

sys.path.insert(0, REPO)
os.environ.setdefault("FOREX_AGENT_HOME", os.path.expanduser("~/.forex-agent"))

from daemon.launch_scanner import feedback  # noqa: E402


def main() -> None:
    proposals = feedback.load_proposals()
    open_entry = [
        p for p in proposals
        if p.get("status") == "open" and p.get("area") == "entries"
    ]
    try:
        with open(MARKER, encoding="utf-8") as f:
            reported = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        reported = set()

    new = [p for p in open_entry if p.get("id") not in reported]
    reported |= {str(p.get("id")) for p in open_entry}
    os.makedirs(os.path.dirname(MARKER), exist_ok=True)
    with open(MARKER, "w", encoding="utf-8") as f:
        json.dump(sorted(reported), f)

    out = {
        "new": [
            {
                "id": p.get("id"),
                "knob": p.get("knob"),
                "current": p.get("current"),
                "suggested": p.get("suggested"),
                "rationale": p.get("rationale"),
                "evidence": p.get("evidence"),
            }
            for p in new
        ],
        "open_entry_count": len(open_entry),
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
