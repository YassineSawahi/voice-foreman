#!/usr/bin/env python3
"""Silent health probe: is the phone's Baresip reachable right now?

Sends a SIP OPTIONS (no ringing) through the baresip line's httpd module, then
looks for the phone's "200 OK" in the line's log. Appends one line per run to
~/.local/state/voice-foreman/probe.log, e.g.

  2026-01-01 12:00:00 reachable=yes reply_ms=20 path=direct app=running

path comes from `tailscale ping`; app comes from ADB when the phone is reachable
there, else "unknown".
"""
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# From ~/.config/voice-foreman/env (vf-probe.service loads it): VF_PHONE is your phone's SIP
# address; VF_PHONE_ADB lists the ADB addresses to try, comma-separated (default: its tailnet one).
PHONE_URI = os.environ.get("VF_PHONE", "")
_host = re.search(r"sips?:(?:[^@>;]*@)?\[?([^\]:;>]+)", PHONE_URI)
PHONE_IP = _host.group(1) if _host else ""
ADB_SERIALS = tuple(s.strip() for s in os.environ.get("VF_PHONE_ADB", f"{PHONE_IP}:5555").split(",") if s.strip())
LINE_LOG = Path.home() / ".local/state/voice-foreman/baresip.log"
PROBE_LOG = Path.home() / ".local/state/voice-foreman/probe.log"


def sip_options(timeout=4.0):
    """Return the reply time in ms if the phone answered 200 OK, else None.

    Goes through baresip's httpd module (127.0.0.1:8000), because ctrl_tcp takes only one
    client and foremand holds it. The command needs its long form ("/options"), fully
    percent-encoded.
    """
    start = LINE_LOG.stat().st_size
    query = urllib.parse.quote(f"/options {PHONE_URI}", safe="")
    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:8000/?{query}", timeout=2):
        while time.monotonic() - t0 < timeout:
            time.sleep(0.05)
            with LINE_LOG.open("rb") as f:
                f.seek(start)
                new = f.read().decode(errors="replace")
            _, _, rest = new.partition(f"OPTIONS of {PHONE_URI}")
            if "\n" in rest:
                status = rest.split("\n", 2)[1]
                return round((time.monotonic() - t0) * 1000) if " 200 " in status else None
    return None


def tailscale_path():
    try:
        out = subprocess.run(["tailscale", "ping", "-c", "1", "--timeout", "3s", PHONE_IP],
                             capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "error"
    if "via DERP" in out:
        return "derp"
    return "direct" if "pong" in out else "none"


def app_state():
    for serial in ADB_SERIALS:
        try:
            r = subprocess.run(["adb", "-s", serial, "shell", "pidof com.tutpro.baresip"],
                               capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return "running"
        if r.returncode == 1 and not r.stderr.strip():
            return "not-running"
    return "unknown"


def save_crash_log(serial):
    """Keep the phone's crash buffer and Baresip's recent lines before restarting it, to find out why."""
    try:
        r = subprocess.run(["adb", "-s", serial, "logcat", "-d", "-v", "time", "-b", "crash,main,system"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return
    keep = [l for l in r.stdout.splitlines()
            if "baresip" in l.lower() or "FATAL" in l or "AndroidRuntime" in l or "lowmemorykiller" in l.lower()]
    if keep:
        out = PROBE_LOG.parent / "phone-logs" / f"{datetime.now():%Y%m%d-%H%M%S}.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(keep[-400:]) + "\n")


def restart_command(serial):
    """The shell command that restarts Baresip, leaving the phone the way it was.

    Baresip starts its SIP service only once its screen is showing. A sleeping phone is woken for
    the restart and put back to sleep. If you're using the phone, nothing is woken or slept, and
    the app you were in comes back to the front afterwards (like tapping its icon).
    """
    state = subprocess.run(["adb", "-s", serial, "shell",
                            "dumpsys power | grep -m1 mWakefulness=; dumpsys window | grep -m1 mFocusedApp; "
                            "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                            "-c android.intent.category.HOME | tail -1"],
                           capture_output=True, text=True, timeout=10).stdout
    return restart_for(state)


def restart_for(state):
    awake = "mWakefulness=Awake" in state
    front = re.search(r"mFocusedApp=.*? u0 ([A-Za-z0-9_.]+)/", state)
    home = re.search(r"^([A-Za-z0-9_.]+)/", state.strip().splitlines()[-1] if state.strip() else "")
    restart = ("am force-stop com.tutpro.baresip; sleep 2; "
               "monkey -p com.tutpro.baresip -c android.intent.category.LAUNCHER 1; sleep 6")
    if not awake:
        return "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard; " + restart + "; input keyevent KEYCODE_SLEEP"
    if front and front.group(1) != "com.tutpro.baresip":
        if home and front.group(1) == home.group(1):
            return restart + "; input keyevent KEYCODE_HOME"
        return restart + f"; monkey -p {front.group(1)} -c android.intent.category.LAUNCHER 1"
    return restart


def repair():
    """Restart the phone's Baresip cleanly over ADB (home Wi-Fi or tailnet). True if a restart ran.

    Seen in practice: after the app restarted itself, its SIP stack never bound 5062 again,
    though the process kept running; a force-stop and fresh launch fixed it.
    """
    for serial in ADB_SERIALS:
        try:
            subprocess.run(["adb", "connect", serial], capture_output=True, timeout=6)
            policy = subprocess.run(["adb", "-s", serial, "shell", "dumpsys window policy"],
                                    capture_output=True, text=True, timeout=10).stdout
            if "secure=true" in policy and "mIsShowing=true" in policy:
                # Locked with a PIN: Baresip can't start its SIP service behind the lock screen, and
                # waking the screen again and again doesn't help. Unlocking the phone
                # (Baresip is left in front) brings it back.
                return "locked"
            save_crash_log(serial)
            r = subprocess.run(["adb", "-s", serial, "shell", restart_command(serial)],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return True
    return False


def main():
    if not PHONE_URI:
        sys.exit("probe: set VF_PHONE (your phone's SIP address) in ~/.config/voice-foreman/env")
    try:
        ms = sip_options()
    except OSError as e:
        line = f"reachable=error line={e.__class__.__name__}"
    else:
        line = f"reachable={'yes' if ms is not None else 'no'} reply_ms={ms if ms is not None else '-'}"
        if ms is None:
            fixed = repair()
            if fixed == "locked":
                line += " repaired=locked"
            elif fixed:
                time.sleep(2)  # Baresip had 6 s to start during the repair
                ms = sip_options()
                line += f" repaired={'yes' if ms is not None else 'failed'}"
    line += f" path={tailscale_path()} app={app_state()}"
    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROBE_LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    print(line)


if __name__ == "__main__":
    main()
