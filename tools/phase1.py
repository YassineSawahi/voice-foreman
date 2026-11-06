#!/usr/bin/env python3
"""Phase 1: the PC calls the phone through baresip and plays one sentence.

Usage: phase1.py [SIP-URI] [--hold SECONDS] [--ring-timeout SECONDS]

Talks to the line over D-Bus (foremand/linebus.py; run it with the project's .venv python),
prints every call event and appends it to ~/.local/state/voice-foreman/phase1.log.
Exits 0 when the call was answered, 1 otherwise.
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "foremand"))
import linebus  # noqa: E402

os.umask(0o077)
LOG = Path.home() / ".local/state/voice-foreman/phase1.log"


async def call(args, log):
    line = linebus.Line()
    await line.connect()
    log(f"--- dialing {args.uri}")
    t0 = time.monotonic()
    reply = (await line.send("dial", args.uri)).strip()
    log(f"response to dial: {reply}")
    if "failed" in reply:
        return None, None, f"dial refused: {reply}"
    rang = answered = closed = None
    hung_up = False
    deadline = args.ring_timeout + args.hold + 30  # never wait forever
    events = line.events()
    while closed is None:
        now = time.monotonic() - t0
        if not hung_up and answered is None and now > args.ring_timeout:
            log("no answer in time: hanging up")
            await line.send("hangup")
            hung_up = True
        if not hung_up and answered is not None and now - answered > args.hold:
            log("hold time over: hanging up")
            await line.send("hangup")
            hung_up = True
        if now > deadline:
            closed = "gave up waiting for CALL_CLOSED"
            break
        try:
            msg = await asyncio.wait_for(anext(events), 0.5)
        except asyncio.TimeoutError:
            continue
        ev = msg.get("type", "?")
        log(f"event {ev} {msg.get('param', '')} peer={msg.get('peeruri', '')}")
        if ev in ("CALL_RINGING", "CALL_PROGRESS") and rang is None:
            rang = now
        elif ev == "CALL_ESTABLISHED":
            answered = now
        elif ev == "CALL_CLOSED":
            closed = msg.get("param", "") or "closed"
    return rang, answered, closed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("uri", nargs="?", default=os.environ.get("VF_PHONE"),
                    help="your phone's SIP address (default: $VF_PHONE)")
    ap.add_argument("--hold", type=float, default=20, help="seconds to stay on after the call is answered")
    ap.add_argument("--ring-timeout", type=float, default=30, help="hang up if not answered by then")
    args = ap.parse_args()
    if not args.uri:
        ap.error("give the phone's SIP address, or set VF_PHONE")

    LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOG.open("a") as out:
        def log(line):
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            print(f"{stamp} {line}", flush=True)
            out.write(f"{stamp} {line}\n")

        rang, answered, closed = asyncio.run(call(args, log))
        ring = f"{rang:.1f} s" if rang is not None else "no ringing reported"
        ans = f"yes, after {answered:.1f} s" if answered is not None else "no"
        log(f"summary: ringing: {ring}; answered: {ans}; ended: {closed}")
    return 0 if answered is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
