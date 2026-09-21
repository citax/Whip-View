"""Claude Code statusline hook for Whip-View.

Claude Code pipes session JSON to this script on stdin. We save the plan
rate-limit windows (5-hour / 7-day) to ~/.claude/whipview-usage.json so the
Whip-View dashboard can show them, and print a short status line.

settings.json:  "statusLine": {"type": "command", "command": "python <path>/claude_statusline.py"}
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

OUT = Path.home() / ".claude" / "whipview-usage.json"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    limits = data.get("rate_limits") or {}
    if limits:
        payload = {"updated": int(time.time()), "rate_limits": limits,
                   "model": (data.get("model") or {}).get("display_name")}
        try:
            tmp = OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(OUT)
        except OSError:
            pass
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {pct:.0f}%")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    print(" | ".join(parts) if parts else "")


if __name__ == "__main__":
    main()
