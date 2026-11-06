#!/usr/bin/env python3
"""Claude Code hook for the voice foreman: spool Stop and Notification events.

Installed in ~/.claude/settings.json as an async hook, so it never blocks a session.
Writes one JSON file per event into ~/.local/state/voice-foreman/events/, which
foremand picks up and deletes. Always exits 0, even if something goes wrong.
The phone brain loads no settings files (--setting-sources ""), so these hooks never run for it
and it never reports itself.
"""
import json
import os
import sys
import time
from pathlib import Path

SPOOL = Path.home() / ".local/state/voice-foreman/events"
FIELDS = ("hook_event_name", "session_id", "cwd", "permission_mode", "background_tasks",
          "last_assistant_message", "notification_type", "title", "message")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if event.get("stop_hook_active"):  # another Stop hook is continuing this turn
        return
    note = {k: event.get(k) for k in FIELDS if event.get(k) is not None}
    if isinstance(note.get("last_assistant_message"), str):
        note["last_assistant_message"] = note["last_assistant_message"][-4000:]
    note["at"] = time.time()
    os.umask(0o077)  # notes hold what your sessions said: yours alone
    SPOOL.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{note.get('hook_event_name', 'event')}.json"
    tmp = SPOOL / f".{name}"
    tmp.write_text(json.dumps(note))
    tmp.rename(SPOOL / name)  # atomic: foremand never reads a half-written file


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a hook must never get in a session's way
        pass
