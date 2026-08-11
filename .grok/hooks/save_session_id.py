#!/usr/bin/env python3
"""Write current Grok session id into the workspace (gitignored).

Used by SessionStart / SessionEnd hooks so you can resume with:
  grok --resume <id>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    session_id = (os.environ.get("GROK_SESSION_ID") or "").strip()
    if not session_id:
        # Fallback: stdin envelope from Grok hooks
        try:
            raw = sys.stdin.read()
            if raw.strip():
                data = json.loads(raw)
                session_id = str(
                    data.get("sessionId")
                    or data.get("session_id")
                    or ""
                ).strip()
        except Exception:
            session_id = ""

    if not session_id:
        # Nothing to write; don't fail the session
        return 0

    root = (
        os.environ.get("GROK_WORKSPACE_ROOT")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    root_path = Path(root)
    out = root_path / ".grok-session"
    event = os.environ.get("GROK_HOOK_EVENT") or "unknown"
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    text = (
        f"# Grok session pointer (local only — do not commit)\n"
        f"# Updated: {now}  event: {event}\n"
        f"#\n"
        f"# Resume this conversation:\n"
        f"#   grok --resume {session_id}\n"
        f"#\n"
        f"{session_id}\n"
    )
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"save_session_id: write failed: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
