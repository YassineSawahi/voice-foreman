#!/usr/bin/env python3
"""Phase 1: the PC calls the phone through baresip and plays one sentence.

Usage: phase1.py [SIP-URI] [--hold SECONDS] [--ring-timeout SECONDS]

Talks to baresip's ctrl_tcp on 127.0.0.1:4444 (netstring-framed JSON),
prints every call event and appends it to ~/.local/state/voice-foreman/phase1.log.
Exits 0 when the call was answered, 1 otherwise.
"""
import argparse
import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path

CTRL = ("127.0.0.1", 4444)
LOG = Path.home() / ".local/state/voice-foreman/phase1.log"


class Ctrl:
    """Minimal ctrl_tcp client: send commands, read responses and events."""

    def __init__(self, addr):
        self.sock = socket.create_connection(addr, timeout=0.5)
        self.buf = b""

    def send(self, command, params=""):
        msg = json.dumps({"command": command, "params": params, "token": command}).encode()
        self.sock.sendall(b"%d:%s," % (len(msg), msg))

    def recv(self):
        """Return the next message, or None if nothing arrived within the timeout."""
        while True:
            head, sep, rest = self.buf.partition(b":")
            if sep and len(rest) > int(head):
                n = int(head)
                self.buf = rest[n + 1:]  # drop the trailing comma
                return json.loads(rest[:n])
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("baresip closed the control socket")
            self.buf += chunk


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("uri", nargs="?", default=os.environ.get("VF_PHONE"),
                    help="your phone's SIP address (default: $VF_PHONE)")
    ap.add_argument("--hold", type=float, default=20, help="seconds to stay on after the call is answered")
    ap.add_argument("--ring-timeout", type=float, default=30, help="hang up if not answered by then")
    args = ap.parse_args()
    if not args.uri:
        ap.error("give the phone's SIP address, or set VF_PHONE")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    ctrl = Ctrl(CTRL)
    with LOG.open("a") as out:
        def log(line):
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            print(f"{stamp} {line}", flush=True)
            out.write(f"{stamp} {line}\n")

        log(f"--- dialing {args.uri}")
        t0 = time.monotonic()
        ctrl.send("dial", args.uri)
        rang = answered = closed = None
        hung_up = False
        deadline = args.ring_timeout + args.hold + 30  # never wait forever
        while closed is None:
            now = time.monotonic() - t0
            if not hung_up and answered is None and now > args.ring_timeout:
                log("no answer in time: hanging up")
                ctrl.send("hangup")
                hung_up = True
            if not hung_up and answered is not None and now - answered > args.hold:
                log("hold time over: hanging up")
                ctrl.send("hangup")
                hung_up = True
            if now > deadline:
                closed = "gave up waiting for CALL_CLOSED"
                break
            msg = ctrl.recv()
            if msg is None:
                continue
            if msg.get("response"):
                data = str(msg.get("data", "")).strip()
                log(f"response to {msg.get('token')}: ok={msg.get('ok')} {data}")
                if msg.get("token") == "dial" and not msg.get("ok"):
                    closed = f"dial refused: {data}"
                continue
            ev = msg.get("type", "?")
            log(f"event {ev} {msg.get('param', '')} peer={msg.get('peeruri', '')}")
            if ev in ("CALL_RINGING", "CALL_PROGRESS") and rang is None:
                rang = now
            elif ev == "CALL_ESTABLISHED":
                answered = now
            elif ev == "CALL_CLOSED":
                closed = msg.get("param", "") or "closed"

        ring = f"{rang:.1f} s" if rang is not None else "no ringing reported"
        ans = f"yes, after {answered:.1f} s" if answered is not None else "no"
        log(f"summary: ringing: {ring}; answered: {ans}; ended: {closed}")
    return 0 if answered is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
