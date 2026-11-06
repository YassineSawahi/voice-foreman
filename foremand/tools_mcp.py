"""The brain's own tools, served to `claude -p` over MCP (JSON-RPC on stdin/stdout).

texting the phone and asking for a callback carry private words (what was said, what's being
worked on). As Bash commands those words would sit on a command line, where any account on this
machine can read them in the process list; through this server they only travel over the pipe
between the brain and this process. foremand starts it via --mcp-config.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with python -I, which leaves the script's folder out
import linebus  # noqa: E402

os.umask(0o077)  # spool notes are private
SPOOL = Path(os.environ.get("VF_SPOOL", Path.home() / ".local/state/voice-foreman/events"))
ACTIONS = Path(os.environ.get("VF_ACTIONS", Path.home() / ".config/voice-foreman/actions.json"))
PIN_OK = Path.home() / ".local/state/voice-foreman/pin_ok"  # foremand writes it when the keypad PIN is right
BUILT_IN = {"lock the PC": {"says": "Lock the PC and turn its screen off", "command": ["omarchy-system-lock"]}}

TOOLS = [
    {
        "name": "text_phone",
        "description": "Text the user's phone now; it shows in Baresip's chat. Short plain text, no markdown.",
        "inputSchema": {"type": "object", "required": ["message"],
                        "properties": {"message": {"type": "string", "description": "The text to send"}}},
    },
    {
        "name": "call_back",
        "description": ("Have foremand call the user back in this many minutes (0: right after this call). "
                        "Other calls wait until then. Returns the time of the callback."),
        "inputSchema": {"type": "object", "required": ["minutes", "reason"],
                        "properties": {
                            "minutes": {"type": "number", "minimum": 0},
                            "reason": {"type": "string",
                                       "description": "What it's about; the callback starts fresh, so say everything needed"}}},
    },
]


def actions():
    """The PC actions you set up in actions.json (plus locking the PC, which is always safe):
    {"name": {"says": "what it does", "command": ["program", "arg"], "pin": true}}.
    The brain picks one by name and can't pass anything else, so it only ever runs your commands."""
    found = {k: v for k, v in BUILT_IN.items() if shutil.which(v["command"][0])}
    try:
        mine = json.loads(ACTIONS.read_text())
    except (OSError, ValueError):
        mine = {}
    for name, spec in mine.items():
        if isinstance(spec, dict) and isinstance(spec.get("command"), list) and spec["command"]:
            found[name] = spec
    return found


def pin_unlocked():
    try:
        return json.loads(PIN_OK.read_text()).get("until", 0) > time.time()
    except (OSError, ValueError):
        return False


def pc_action(name):
    spec = actions().get(name)
    if not spec:
        return f"No PC action called {name!r}. The ones set up: {', '.join(actions()) or 'none'}.", True
    if spec.get("pin") and not pin_unlocked():
        return ("Locked: this one needs the keypad PIN. Ask the user to type their PIN and press #, "
                "then try again."), True
    try:
        r = subprocess.run([str(a) for a in spec["command"]], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Didn't work: {e}", True
    out = (r.stdout + r.stderr).strip()[-1500:]
    if r.returncode != 0:
        return f"Failed (exit code {r.returncode}). {out}".strip(), True
    return out or "Done.", False


def tool_list():
    """The tools, with pc_action describing whatever actions are set up right now."""
    found = actions()
    if not found:
        return TOOLS
    lines = [f"- {n}: {s.get('says', n)}" + (" (needs the keypad PIN)" if s.get("pin") else "") for n, s in found.items()]
    pc = {"name": "pc_action",
          "description": "Run one of the PC actions the user set up, by name. Returns what it printed.\n" + "\n".join(lines),
          "inputSchema": {"type": "object", "required": ["name"],
                          "properties": {"name": {"type": "string", "enum": list(found)}}}}
    return TOOLS + [pc]


def text_phone(message):
    message = " ".join(str(message).split())
    if not message:
        return "Nothing to send: the message was empty.", True
    reply = linebus.invoke("message", message).strip()
    return (reply or "Texted."), False


def call_back(minutes, reason):
    minutes = max(0.0, float(minutes))
    SPOOL.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{time.time_ns()}-Callback.json"
    (SPOOL / f".{name}").write_text(json.dumps(
        {"hook_event_name": "Callback", "minutes": minutes, "reason": str(reason), "at": time.time()}))
    (SPOOL / f".{name}").rename(SPOOL / name)  # atomic for foremand
    return "Callback set for " + time.strftime("%H:%M", time.localtime(time.time() + minutes * 60)), False


def handle(method, params):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "voice-foreman", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        args = params.get("arguments") or {}
        try:
            if params.get("name") == "text_phone":
                text, is_error = text_phone(args.get("message", ""))
            elif params.get("name") == "call_back":
                text, is_error = call_back(args.get("minutes", 0), args.get("reason", ""))
            elif params.get("name") == "pc_action":
                text, is_error = pc_action(str(args.get("name", "")))
            else:
                text, is_error = f"No such tool: {params.get('name')}", True
        except (linebus.LineError, OSError, ValueError) as e:
            text, is_error = f"Didn't work: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    raise LookupError(method)


def main():
    for raw in sys.stdin:
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if "id" not in req:  # a notification: nothing to answer
            continue
        out = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            out["result"] = handle(req.get("method"), req.get("params") or {})
        except LookupError:
            out["error"] = {"code": -32601, "message": f"Method not found: {req.get('method')}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
