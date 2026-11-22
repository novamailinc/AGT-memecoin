#!/usr/bin/env python3
"""Send a Telegram message via the stored custom.telegram credential.

Usage: send.py --chat-id <id> --text <message> [--parse-mode Markdown]

The bot token never appears here: authd swaps a surrogate into the URL path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

# dynamic_credentials lives in the connector-setup skill's bin/ dir.
# The old templates/skill/bin path no longer exists (skill reorganised)
# and broke this script with ModuleNotFoundError — try the canonical
# path first, keep the old one as a fallback.
for _p in ("/opt/muse/skills/custom-connector-setup/bin",
           "/opt/muse/skills/custom-connector-setup/templates/skill/bin"):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from dynamic_credentials import (  # noqa: E402
    read_json_response,
    url_with_surrogate_path_segment,
)

ALLOWED_HOSTS = ("api.telegram.org",)
CREDENTIAL = "custom.telegram"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--parse-mode", default="")
    args = ap.parse_args()

    url = url_with_surrogate_path_segment(
        "https://api.telegram.org/bot{}/sendMessage",
        CREDENTIAL,
        allowed_hosts=ALLOWED_HOSTS,
    )
    payload = {"chat_id": args.chat_id, "text": args.text}
    if args.parse_mode:
        payload["parse_mode"] = args.parse_mode
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = read_json_response(resp)
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": bool(body.get("ok")), "message_id": (body.get("result") or {}).get("message_id")}))
    return 0 if body.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
