#!/usr/bin/env python3
"""foremand: the voice foreman's call handler (Phase 2: the talk loop).

Owns one baresip line through D-Bus (foremand/linebus.py: baresip's ctrl_dbus on your session
bus, so no other account on this machine can drive it). While a call is up it bridges the call audio
to the speech service and a Claude brain:

  you    -> baresip -> vf_rx sink -> parec -> speech /listen (VAD + whisper) -> brain
  brain  -> sentences -> speech /tts (Kokoro) -> mixer (+ filler, music) -> pacat -> vf_tx -> baresip

Everything is configured with VF_* environment variables (see CONFIG below).
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import websockets

import linebus

os.umask(0o077)  # logs, transcripts, recaps and state are yours alone

PROJECT = Path(__file__).resolve().parent.parent
CONFIG = {
    "speech": os.environ.get("VF_SPEECH", "127.0.0.1:8765"),
    "rx_source": os.environ.get("VF_RX_SOURCE", "vf_rx.monitor"),
    "tx_sink": os.environ.get("VF_TX_SINK", "vf_tx"),
    # desk talks: the PC's own microphone and speakers (pulse names; @DEFAULT_..@ follows your defaults)
    "desk_source": os.environ.get("VF_DESK_SOURCE", "@DEFAULT_SOURCE@"),
    "desk_sink": os.environ.get("VF_DESK_SINK", "@DEFAULT_SINK@"),
    # no echo canceller at the desk: the mic stays shut while Omarchy talks, and this long after
    "desk_echo_tail": float(os.environ.get("VF_DESK_ECHO_TAIL", "0.6")),
    "model": os.environ.get("VF_MODEL", "claude-opus-5-5"),
    "effort": os.environ.get("VF_EFFORT", "medium"),
    "fallback": os.environ.get("VF_FALLBACK", "claude-sonnet-5-5"),
    "voice": os.environ.get("VF_VOICE", ""),  # empty: the speech service default
    # The plan says filler at ~1.5 s and music at ~3 s; replies usually land 1.5-2.7 s
    # after you stop talking, so start later to avoid "one sec" right before an answer.
    "filler_after": float(os.environ.get("VF_FILLER_AFTER", "2.5")),
    "music_after": float(os.environ.get("VF_MUSIC_AFTER", "4.0")),
    # silence after a heads-up ("Let me check.") while the brain is still working
    "music_after_heads_up": float(os.environ.get("VF_MUSIC_AFTER_HEADS_UP", "1.5")),
    # calling you (see Dispatcher): your phone's SIP address, e.g. sip:phone@100.64.0.2:5062
    "phone": os.environ.get("VF_PHONE", ""),
    # the bar widget that reports whether the PC has been idle for 30 s (omarchy-phone)
    "idle_ipc": os.environ.get("VF_IDLE_IPC", "io.github.yassinesawahi.phone"),
    # Who may call in: hosts, comma-separated; empty means only the phone's host. Only a filter:
    # the caller writes its own From. The lock is the line's secret address (see refusal()).
    "allowed_callers": os.environ.get("VF_ALLOWED_CALLERS", ""),
    # the line's secret, written by tools/configure-line.sh; its address is sip:vf-<secret>@this-PC
    "line_secret": os.environ.get("VF_LINE_SECRET", str(Path.home() / ".config/voice-foreman/line-secret")),
    "notify": os.environ.get("VF_NOTIFY", "1") != "0",  # desktop notifications; tests turn them off
    "away_after": float(os.environ.get("VF_AWAY_AFTER", "180")),  # seconds idle before you count as away
    # VF_ALWAYS_CALL=1/0 forces the mode (tests); unset, the bar's settings.json decides.
    "always_call": {"1": True, "0": False}.get(os.environ.get("VF_ALWAYS_CALL", "")),
    "urgent_wait": float(os.environ.get("VF_URGENT_WAIT", "10")),  # a session needs you
    "batch_wait": float(os.environ.get("VF_BATCH_WAIT", "90")),  # a session finished: wait for others
    "ring_timeout": float(os.environ.get("VF_RING_TIMEOUT", "45")),
    "hello_wait": float(os.environ.get("VF_HELLO_WAIT", "1.5")),  # outgoing: wait for their "hello?"
    "unfinished_wait": float(os.environ.get("VF_UNFINISHED_WAIT", "1.2")),  # "a report about the...": more coming?
    "after_call_minutes": float(os.environ.get("VF_AFTER_CALL_MINUTES", "15")),  # work promised on the call
    "calls_override": {"1": True, "0": False}.get(os.environ.get("VF_CALLS", "")),  # tests: ignore the bar switch
    "retry_after": float(os.environ.get("VF_RETRY_AFTER", "900")),  # declined or missed
}
PROMPT_FILE = PROJECT / "foremand/prompts/brain.md"

# What the brain may do on a call. dontAsk mode refuses everything not allowed here.
# Reads run straight away; acting goes through Claude Code sessions (SendMessage, claude --bg),
# which the persona confirms out loud first, and which keep their own permission checks.
BRAIN_TOOLS = "Bash,Read,ListAgents,SendMessage,WebSearch"
# Read-only with any arguments: none of these has an option that writes, deletes, or runs something
# (git's few exceptions are in BRAIN_DENIED). File contents go through Read; Claude Code also runs
# plain reads like head and cat by itself, and holds them to BRAIN_DENIED's Read rules too.
READ_ONLY_ANY_ARGS = [
    "ls", "du", "df", "wc", "stat", "uname", "uptime", "free", "which", "ps", "pgrep", "lsblk",
    "git status", "git log", "git diff", "git show",
    "docker ps", "systemctl --user status", "systemctl --user list-units", "claude logs",
]
# Read-only only exactly as written. Given other arguments these delete or rename branches
# (git branch -D/-m), write files (nvidia-smi -f), change settings (sensors -s, date -s, hostname),
# serve a web page (tailscale status --web), or dispatch sessions (claude agents --dangerously-…).
READ_ONLY_EXACT = [
    "date", "whoami", "hostname", "nvidia-smi", "sensors", "omarchy version",
    "git branch", "git branch --show-current", "git branch --list", "git branch -a", "git branch -r",
    "git branch -v", "git branch -vv",
    "tailscale status", "tailscale status --json", "tailscale status --active",
    "claude agents --json", "claude agents --json --all",
]
BRAIN_ALLOWED = (["Read", "ListAgents", "SendMessage", "WebSearch", "Bash(cd *)", "Bash(claude --bg *)",
                  "mcp__vf__text_phone", "mcp__vf__call_back", "mcp__vf__pc_action",
                  f"Bash({PROJECT}/bin/vf-session *)", f"Bash({PROJECT}/bin/vf-pref *)",
                  f"Bash({PROJECT}/bin/vf-stop-session *)"]
                 + [f"Bash({c})" for c in READ_ONLY_ANY_ARGS + READ_ONLY_EXACT]
                 + [f"Bash({c} *)" for c in READ_ONLY_ANY_ARGS])
# Deny wins over allow. git log/diff/show can write their output to a file (--output), read a
# file outside the repository (--no-index), or run a configured diff program (--ext-diff).
BRAIN_DENIED = ["Read(~/.ssh/**)", "Read(~/.gnupg/**)", "Read(~/.claude/.credentials.json)",
                "Read(**/.env)", "Read(**/.env.*)", "Read(~/.password-store/**)",
                "Bash(git *--output*)", "Bash(git *--no-index*)", "Bash(git *--ext-diff*)"]
HOLD_MUSIC = PROJECT / "sounds/hold.wav"
STATE = Path(os.environ.get("VF_STATE", Path.home() / ".local/state/voice-foreman"))  # tests use their own
LOG = STATE / "foremand.log"
SETTINGS = Path.home() / ".config/voice-foreman/settings.json"  # written by bin/vf-ctl (the bar plugin)
STATUS = STATE / "status.json"  # read by bin/vf-ctl
SESSIONS = STATE / "sessions.json"  # each session's latest note, for the brain
DEFAULTS = {"calls": True, "mode": "always", "quiet": None, "respect_dnd": True, "pin": None}
CALLS_LOG = Path(os.environ.get("VF_CALLS_LOG", Path.home() / ".local/state/voice-foreman/calls.jsonl"))  # one recap per call: Omarchy's memory
PREFS = Path.home() / ".config/voice-foreman/prefs.json"  # per session: {"word": "mute" | "text" | "call"}
PIN_OK = Path.home() / ".local/state/voice-foreman/pin_ok"  # keypad PIN entered (bin/vf-stop-session checks it)


def recent_calls(limit=5, hours=24):
    """The last few call recaps, for the next call's opening."""
    try:
        lines = CALLS_LOG.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if time.time() - c.get("at", 0) < hours * 3600 and c.get("recap"):
            out.append(f"({time.strftime('%H:%M', time.localtime(c['at']))}) {c['recap']}")
    return out


def session_pref(note):
    """'mute', 'text', 'call' or None, from prefs.json words matched against the session's folder."""
    try:
        prefs = json.loads(PREFS.read_text())
    except (OSError, ValueError):
        return None
    where = (note.get("cwd") or "").lower().replace("_", "").replace("-", "")
    for word, pref in prefs.items():
        if word.lower().replace("_", "").replace("-", "").replace(" ", "") in where:
            return pref
    return None


def in_quiet_hours(window, now=None):
    """window like "22:00-07:00"; True inside it (overnight windows work)."""
    try:
        start, end = (time.strptime(t.strip(), "%H:%M") for t in window.split("-"))
    except (AttributeError, ValueError):
        return False
    t = time.localtime(now or time.time())
    minute, a, b = t.tm_hour * 60 + t.tm_min, start.tm_hour * 60 + start.tm_min, end.tm_hour * 60 + end.tm_min
    return a <= minute < b if a < b else minute >= a or minute < b


def settings():
    """Your switches from the bar: calls on/off, and always vs only when you're away."""
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)

OUT_RATE = 24000  # Kokoro's native rate; pacat plays it into vf_tx
FRAME = OUT_RATE // 50  # 20 ms
IN_RATE = 16000
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
# What you said trails off ("text me a report about the..."): the pause was you thinking, not the end.
UNFINISHED = re.compile(r"(?:\.\.\.|…|,"
                        r"|\b(?:the|a|an|my|your|and|or|but|because|um|uh)\W*"  # never end a sentence
                        r"|\b(?:to|of|about|for|with|from)[\s.]*"  # can end a question: "what's it for?"
                        r")\s*$", re.I)
BYE = "[bye]"
DESK, PHONE = "[desk]", "[phone]"  # "move to my PC" / "call me": carry the conversation over
MARKERS = (BYE, DESK, PHONE)
DISPATCHER = None  # set in main(); a conversation uses it to end a desk talk or ring the phone
AFTER_CALL = (
    "[call ended] The user hung up. If you told them you'd do something after the call (look something up, "
    "write a report, hand work to a session), or the call ended while you were still answering them, do it now "
    "with your tools and text them the result or answer with the text_phone tool (short, plain text). The recap must only say "
    "what really happened on the call. Then end with a short recap of the call for their phone as your final reply: two to "
    "four lines, plain text, starting with \"Omarchy:\". It's texted to them automatically and kept as your memory "
    "of the call. If your text_phone messages already covered everything, start it with \"SKIP \" so it's kept but "
    "not texted again.")
TAG = re.compile(r"<[^>]*>")
CODE = re.compile(r"[<>]|=\"|\$\(|&&|\||`|~/|/dev/|\bname=\"|^\s*[{\[]|[{}]")
NOT_SPOKEN = "Sorry, I can't do that from the phone yet."


def speakable(sentence):
    """The sentence as it should be spoken, or None if it's code, a command or markup."""
    if CODE.search(sentence):
        return None
    text = TAG.sub("", sentence).replace("*", "").replace("#", "").strip()
    return text if re.search(r"[A-Za-z0-9]", text) else None


def log(msg):
    msg = " ".join(str(msg).split("\n"))  # session messages can hold newlines; keep one line per entry
    line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d} {line}\n")


# --------------------------------------------------------------------------- speech API

# Words Kokoro says wrong, respelled for the voice only; the brain and the logs keep the real spelling.
PRONOUNCE = {"Omarchy": "Oh-marshey"}


def for_voice(text):
    for word, sound in PRONOUNCE.items():
        text = re.sub(rf"\b{word}\b", sound, text, flags=re.IGNORECASE)
    return text


def tts(text):
    """Kokoro through the speech service: returns int16 samples at OUT_RATE."""
    body = {"text": for_voice(text)}
    if CONFIG["voice"]:
        body["voice"] = CONFIG["voice"]
    req = urllib.request.Request(f"http://{CONFIG['speech']}/tts", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rate = int(r.headers.get("X-Sample-Rate", OUT_RATE))
        pcm = np.frombuffer(r.read(), dtype="<i2")
    if rate != OUT_RATE:  # cheap linear resample; Kokoro is 24 kHz so this rarely runs
        x = np.linspace(0, len(pcm) - 1, int(len(pcm) * OUT_RATE / rate))
        pcm = np.interp(x, np.arange(len(pcm)), pcm).astype("<i2")
    return pcm


def stt_hints():
    """Words Whisper should expect on this call: your session and project names, plus regulars."""
    words = ["Omarchy", "Claude", "session", "sessions", "text me", "report", "recap"]
    # your own regulars (project names, how you like to be called), comma-separated
    words += [w.strip() for w in os.environ.get("VF_STT_WORDS", "").split(",") if w.strip()]
    try:
        out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=8).stdout
        for agent in json.loads(out):
            if agent.get("name") == BRAIN_NAME:
                continue
            for raw in (agent.get("name", ""), Path(agent.get("cwd", "")).name):
                name = re.sub(r"[-_]+", " ", re.sub(r"-[0-9a-f]{1,4}$", "", raw)).strip()
                if name and name not in words:
                    words.append(name)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "Vocabulary: " + ", ".join(words) + "."


def warm_stt():
    """Half a second of silence through speech-to-text, to wake the GPU path up."""
    req = urllib.request.Request(f"http://{CONFIG['speech']}/stt", data=bytes(IN_RATE))
    urllib.request.urlopen(req, timeout=20).read()


def hold_music():
    """Your track from sounds/hold.wav (mono/stereo 16-bit, any rate), else a soft placeholder pad."""
    if HOLD_MUSIC.exists():
        with wave.open(str(HOLD_MUSIC)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
            if w.getnchannels() == 2:
                pcm = pcm.reshape(-1, 2).mean(axis=1)
            rate = w.getframerate()
        x = np.linspace(0, len(pcm) - 1, int(len(pcm) * OUT_RATE / rate))
        return np.interp(x, np.arange(len(pcm)), pcm).astype(np.float32)
    t = np.arange(OUT_RATE * 16) / OUT_RATE  # 16 s loop: four slow chords
    chords = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (196.0, 246.9, 329.6)]
    out = np.zeros_like(t)
    for i, chord in enumerate(chords):
        seg = (t >= i * 4) & (t < (i + 1) * 4)
        env = np.sin(np.pi * (t[seg] - i * 4) / 4) ** 2
        out[seg] = sum(np.sin(2 * np.pi * f * t[seg]) for f in chord) * env
    return (out / np.abs(out).max() * 6000).astype(np.float32)


# --------------------------------------------------------------------------- audio out

class Mixer:
    """Plays voice (always on top) and hold music into vf_tx in 20 ms frames, with fades."""

    def __init__(self, sink):
        self.sink = sink  # vf_tx on a call, the PC's speakers at the desk; can change mid-conversation
        self.voice = deque()  # int16 arrays
        self.offset = 0  # samples already played of voice[0]
        self.music = hold_music()
        self.music_pos = 0
        self.music_gain = 0.0
        self.music_target = 0.0
        self.voice_gain = 1.0
        self.fading_voice = False
        self.last_voice = 0.0  # monotonic time the voice last played; 0 = not yet on this call
        self.held = False  # True while an outgoing call rings: voice queues up but does not play

    @property
    def speaking(self):
        return bool(self.voice)

    def say(self, pcm):
        self.voice.append(pcm)
        self.music_target = 0.0
        self.fading_voice, self.voice_gain = False, 1.0

    def stop_voice(self):
        """Barge-in: fade the voice out over ~60 ms, then drop what's queued."""
        if self.voice:
            self.fading_voice = True

    def set_music(self, on):
        self.music_target = 0.35 if on else 0.0

    def _voice_frame(self):
        out = np.zeros(FRAME, dtype=np.float32)
        if self.held:  # an outgoing call still ringing, or waiting for "hello?": keep the opening queued
            return out
        filled = 0
        while filled < FRAME and self.voice:
            chunk = self.voice[0]
            take = min(FRAME - filled, len(chunk) - self.offset)
            out[filled:filled + take] = chunk[self.offset:self.offset + take]
            filled += take
            self.offset += take
            if self.offset >= len(chunk):
                self.voice.popleft()
                self.offset = 0
        if filled:
            self.last_voice = time.monotonic()
        if self.fading_voice:
            gains = np.linspace(self.voice_gain, max(self.voice_gain - 1 / 3, 0), FRAME)
            out *= gains
            self.voice_gain = gains[-1]
            if self.voice_gain <= 0:
                self.voice.clear()
                self.offset, self.fading_voice, self.voice_gain = 0, False, 1.0
        return out

    def _music_frame(self):
        # ramp 0 -> 0.35 in ~0.5 s, back down in ~0.3 s
        start = self.music_gain
        if self.music_target > start:
            self.music_gain = min(self.music_target, start + 0.35 / 25)
        else:
            self.music_gain = max(self.music_target, start - 0.35 / 15)
        if start == 0 and self.music_gain == 0:
            return 0.0
        idx = (self.music_pos + np.arange(FRAME)) % len(self.music)
        self.music_pos = (self.music_pos + FRAME) % len(self.music)
        return self.music[idx] * np.linspace(start, self.music_gain, FRAME)

    async def run(self):
        proc, playing_to = None, None
        t0, n = time.monotonic(), 0
        try:
            while True:
                if self.sink != playing_to:  # first frame, or the conversation moved
                    if proc:
                        proc.kill()
                    proc = await asyncio.create_subprocess_exec(
                        "pacat", "--playback", f"--device={self.sink}", "--format=s16le",
                        f"--rate={OUT_RATE}", "--channels=1", "--raw", "--latency-msec=60",
                        stdin=asyncio.subprocess.PIPE)
                    playing_to = self.sink
                frame = self._voice_frame() + self._music_frame()
                proc.stdin.write(np.clip(frame, -32768, 32767).astype("<i2").tobytes())
                await proc.stdin.drain()
                n += 1
                await asyncio.sleep(max(0.0, t0 + n * 0.02 - time.monotonic()))
        finally:
            if proc:
                proc.kill()


# --------------------------------------------------------------------------- brain

BRAIN_NAME = "omarchy-voice"  # how the brain shows up in `claude agents`, so it can leave itself out


class Brain:
    """A `claude -p` process in stream-json mode, one per call."""

    def __init__(self, on_text, on_block_end, on_turn_end):
        self.on_text, self.on_block_end, self.on_turn_end = on_text, on_block_end, on_turn_end
        self.proc = None
        self.consumed = 0  # our messages the brain has read: each one is echoed back as it's read
        self.replays = False  # seen an echo (if this claude never echoes, replies are counted instead)

    async def start(self):
        # Isolated from your own Claude Code setup: no settings files (so none of your hooks, plugins or
        # permission rules), no CLAUDE.md or auto-memory, no skills; only its own tools server.
        # (--safe-mode would also drop that server.) The server inherits this env, D-Bus address included.
        tools = {"mcpServers": {"vf": {"type": "stdio", "command": sys.executable,
                                        "args": ["-I", str(PROJECT / "foremand/tools_mcp.py")],
                                        "env": {"VF_SPOOL": str(SPOOL)}}}}
        cmd = ["claude", "-p", "--setting-sources", "", "--settings", json.dumps({"autoMemoryEnabled": False}),
               "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", json.dumps(tools),
               "--no-session-persistence", "--name", BRAIN_NAME,
               "--model", CONFIG["model"], "--effort", CONFIG["effort"],
               "--fallback-model", CONFIG["fallback"],
               "--tools", BRAIN_TOOLS, "--permission-mode", "dontAsk",
               "--allowedTools", *BRAIN_ALLOWED, "--disallowedTools", *BRAIN_DENIED,
               "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--replay-user-messages",
               "--append-system-prompt", PROMPT_FILE.read_text().replace("{VF_BIN}", str(PROJECT / "bin"))]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=2**24, cwd=Path.home())
        asyncio.create_task(self._read())

    def send(self, text):
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def _read(self):
        async for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("type") == "stream_event":
                event = ev.get("event", {})
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.on_text(delta.get("text", ""))
                elif event.get("type") == "content_block_stop":
                    self.on_block_end()  # e.g. "Let me check." right before a tool call
            elif ev.get("type") == "assistant":  # log what the brain does, not just what it says
                for block in ev.get("message", {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        args = block.get("input") or {}
                        what = args.get("command") or args.get("file_path") or args.get("to") or args.get("query") or ""
                        log(f"  tool: {block.get('name')} {' '.join(str(what).split())[:160]}")
            elif ev.get("type") == "user" and ev.get("isReplay"):  # (tool results are "user" events too)
                self.consumed += 1
                self.replays = True
            elif ev.get("type") == "result":
                self.on_turn_end()

    def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


# --------------------------------------------------------------------------- one call

class Call:
    def __init__(self, line, call_id, direction, peer, agenda=None, where="phone"):
        self.line, self.call_id, self.direction, self.peer = line, call_id, direction, peer
        self.where = where  # "phone" (a SIP call) or "desk" (the PC's mic and speakers)
        self.moved = False  # carried over between phone and desk at least once
        self.listener = None  # the _listen task for the current microphone
        self.move_after_voice = None  # "desk" / "phone" once the current sentence has played
        self.agenda = agenda  # what happened in your sessions, for the opening
        self.after_call = False  # the user hung up: the brain finishes promised work, then the recap
        self.digits = ""  # keypad digits typed so far (PIN entry)
        self.last_reply, self.reply_turn = [], None  # audio of the latest answer, for "1" on the keypad
        self.block = self.final_block = ""  # after the call: text of the current / last text block
        self.hearing_speech = False
        self.mixer = Mixer(self._devices()[1])
        self.brain = Brain(self._brain_text, self._brain_block_end, self._brain_turn_end)
        self.sentences = asyncio.Queue()
        self.pending = ""  # brain text not yet split into a sentence
        self.queued = self.skipped = 0  # sentences of the current reply: spoken / filtered out
        self.sent = self.done = 0  # brain turns sent / answered
        self.drop_through = None  # after a barge-in: ignore the brain's text for turns <= this
        self.turn = 0  # speaking generation; bumped on barge-in so queued sentences are dropped
        self.voiced = deque(maxlen=12)  # (pcm, sentence) handed to the mixer, to name what a barge-in cut
        self.in_tts = None  # the sentence being voiced right now
        self.cut_at = None  # a barge-in lost part of an answer: the sentence it cut, told to the brain next
        self.unheard = ""  # the brain's words that barge-in dropped, for the log
        self.waiting_since = None  # you stopped talking, no reply audio yet
        self.filler = None
        self.hints = None  # Whisper vocabulary for this call
        self.hangup_after_voice = False
        self.tasks = []

    def _devices(self):
        """(microphone, speaker) for where the conversation is now."""
        if self.where == "desk":
            return CONFIG["desk_source"], CONFIG["desk_sink"]
        return CONFIG["rx_source"], CONFIG["tx_sink"]

    def _opening(self):
        if self.direction == "desk":
            opening = ("[desk started] The user is at the PC and started a voice chat with you through the PC's "
                       "microphone and speakers (no phone call). Greet them in one short sentence.")
            if self.agenda:
                opening += f" Then tell them what happened in their sessions: {self.agenda}"
        elif self.direction == "incoming":
            opening = "[call started] The user just called you. Greet them in one short sentence."
            if self.agenda:
                opening += f" Then tell them what happened while they were away: {self.agenda}"
        elif self.agenda:
            opening = (f"[call agenda] You are calling the user because of this: {self.agenda} "
                       "They are picking up; your words play right after their \"hello?\". "
                       "Open with \"Omarchy here\", give each item in a sentence "
                       "or two (anything waiting for them first), then ask what they want to do next.")
        else:
            opening = "[call agenda] You are calling the user to test the line. Say hi in one short sentence."
        memory = recent_calls()
        if memory:
            opening += (" [memory] Your last calls with the user today, oldest first: " + " ".join(memory)
                        + " Use this if they refer back to earlier calls; don't recite it.")
        return opening

    async def prepare(self, hold=False):
        """Brain, voice and the opening. For calls we place this runs while the phone rings, with
        the opening held in the mixer until they pick up and say "hello?"."""
        self.mixer.held = hold
        await self.brain.start()
        self.filler = await asyncio.to_thread(tts, "One sec.")
        await asyncio.to_thread(warm_stt)  # the first real sentence shouldn't pay for a cold GPU
        self.hints = await asyncio.to_thread(stt_hints)
        self.tasks += [asyncio.create_task(t) for t in (self.mixer.run(), self._speak(), self._watch())]
        opening = self._opening()
        log(f"  opening: {opening[:200]}")
        self._user_turn(opening, wait=False)

    def go_live(self, call_id):
        """The call connected: start listening, and release a held opening after their hello."""
        self.call_id = call_id
        log(f"call {call_id}: {self.direction} {self.peer}")
        self.listener = asyncio.create_task(self._listen())
        self.tasks.append(self.listener)
        if self.mixer.held:
            self.tasks.append(asyncio.create_task(self._release_after_hello()))

    async def start(self):
        """A call to us: everything starts now."""
        await self.prepare()
        self.go_live(self.call_id)

    async def _release_after_hello(self):
        await asyncio.sleep(CONFIG["hello_wait"])
        while self.hearing_speech and self.mixer.held:  # mid-"hello": let them finish
            await asyncio.sleep(0.1)
        self.mixer.held = False

    def on_key(self, key):
        """Keypad: 1 repeats the last answer, 0 stops talking, 9 hangs up; digits then # enter the PIN."""
        log("  key: digit" if key.isdigit() else f"  key: {key}")  # never the digits: the PIN is typed here
        if key == "#":
            pin = settings().get("pin")
            entered = self.digits
            self.digits = ""
            if pin and entered == str(pin):
                PIN_OK.write_text(json.dumps({"until": time.time() + 600}))
                self.brain.send("[keypad] The user typed the correct PIN: you may stop a session for the next "
                                "ten minutes if they ask (vf-stop-session).")
                self.sent += 1
                self.mixer.say(self.filler)  # "One sec." doubles as an audible ack
            else:
                self.brain.send("[keypad] The user typed a wrong PIN (or none is set). Tell them briefly.")
                self.sent += 1
        elif key == "1" and self.last_reply:
            for pcm in self.last_reply:
                self.mixer.say(pcm)
        elif key == "0":
            self._barge_in()
            self.cut_at = None  # they asked for quiet: don't bring it back
        elif key == "9" and not self.digits:
            asyncio.create_task(self.hang_up())
        elif key.isdigit():
            self.digits += key

    def update(self, news):
        """Something happened in a session during the call: the brain mentions it at the next pause."""
        log(f"  update: {news[:200]}")
        if self.brain.proc is None:  # still starting: fold it into the opening
            self.agenda = f"{self.agenda} {news}" if self.agenda else news
            return
        self.brain.send(f"[update] While you were talking: {news} Mention it briefly at the next natural pause.")
        self.sent += 1

    async def finish(self, answered=True):
        """Hang-up: stop the audio, let the brain finish what it promised and text a recap, then stop it."""
        for t in self.tasks:
            t.cancel()
        log(f"call {self.call_id}: ended")
        if answered and (self.sent > 1 or self.agenda) and self.brain.proc and self.brain.proc.returncode is None:
            text = (await self._after_call() or "").strip()
            skip = text.upper().startswith("SKIP")
            recap = text[4:].strip(" :") if skip else text
            if recap:
                with CALLS_LOG.open("a") as f:
                    f.write(json.dumps({"at": time.time(), "direction": self.direction, "recap": recap}) + "\n")
            if recap and not skip and self.where != "desk":
                await self.line.send("message", recap)
                log(f"  recap texted: {recap[:200]}")
            elif recap:
                log("  recap kept, not texted (" + ("a desk talk" if self.where == "desk" else "the texts already covered it") + ")")
            else:
                log("  no recap")
        self.brain.stop()

    async def _after_call(self):
        deadline = time.monotonic() + 60
        while self.done < self.sent and time.monotonic() < deadline:  # a reply in progress finishes first
            await asyncio.sleep(0.2)
        self.after_call, self.block, self.final_block = True, "", ""
        self.after_done = asyncio.Event()
        self.sent += 1
        self.brain.send(AFTER_CALL)
        log("  after the call: finishing promised work, then the recap")
        try:
            await asyncio.wait_for(self.after_done.wait(), CONFIG["after_call_minutes"] * 60)
        except asyncio.TimeoutError:
            log("  after-call work ran out of time")
        return " ".join(self.final_block.split())[:600] or None

    def _user_turn(self, text, wait=True):
        if self.cut_at:  # the brain thinks it said all of its last answer: it didn't get through
            text = (f'[interrupted] The user talked over you while you were saying "{self.cut_at}", so they '
                    "didn't hear the rest of that answer. Answer what they say now; if something they missed "
                    "still matters, add it in a sentence.\n" + text)
            self.cut_at = None
        self.queued = self.skipped = 0  # sentences of this reply: spoken / filtered out
        self.sent += 1
        self.waiting_since = time.monotonic() if wait else None
        self.brain.send(text)

    async def hang_up(self):
        """End the conversation: hang up the call, or close a desk talk."""
        if self.where == "desk":
            DISPATCHER.end_desk(self)
        else:
            await self.line.send("hangup")  # CALL_CLOSED follows, and the main loop calls finish()

    async def move(self, where):
        """Carry the conversation over: "move to my PC" ([desk]) or "call me" ([phone])."""
        log(f"  moving the conversation to the {'PC' if where == 'desk' else 'phone'}")
        self.moved = True
        if self.listener:
            self.listener.cancel()
            self.listener = None
        self.where = where
        self.mixer.set_music(False)
        self.mixer.sink = self._devices()[1]
        if where == "desk":
            self.listener = asyncio.create_task(self._listen())
            self.tasks.append(self.listener)
            self._user_turn("[moved to the PC] The phone call has ended; the conversation continues on the PC's "
                            "speakers and microphone. Say one short sentence so they know you're here.", wait=False)
            await self.line.send("hangup")  # only the phone leg: the main loop sees we're at the desk now
        else:
            self.mixer.held = True  # until they pick up and say hello
            self.peer = CONFIG["phone"]
            self._user_turn("[moving to the phone] You're calling the user's phone to carry on this conversation. "
                            "Your words play once they answer; say one short sentence.", wait=False)
            DISPATCHER.ring_for(self)

    def _barge_in(self):
        log("  (barge-in)")
        names = {id(pcm): sentence for pcm, sentence in self.voiced}
        playing = names.get(id(self.mixer.voice[0])) if self.mixer.voice else None
        lost = [names[id(pcm)] for pcm in list(self.mixer.voice)[1:] if id(pcm) in names]
        if self.in_tts:
            lost.append(self.in_tts)
        while not self.sentences.empty():  # written but not voiced yet
            lost.append(self.sentences.get_nowait()[1])
        self.turn += 1
        self.mixer.stop_voice()
        self.hangup_after_voice = False
        self.move_after_voice = None
        if self.done < self.sent:  # the brain is still answering (or hasn't started): drop the rest of it
            self.drop_through = self.sent
        if lost or self.done < self.sent:  # part of an answer will never be heard: tell the brain
            self.cut_at = playing or self.cut_at or "the start of your answer"
        self.unheard = " ".join([self.unheard, *lost]).strip()
        self.unheard += " " if self.unheard else ""  # the brain's next words don't run into these
        self.pending = ""

    def _dropping(self):
        return self.drop_through is not None and self.done < self.drop_through

    # brain -> sentences
    def _queue_sentence(self, sentence):
        if any(m in sentence for m in MARKERS):  # keep the marker; _speak acts on it
            self.sentences.put_nowait((self.turn, sentence))
            return
        text = speakable(sentence)
        if text is None:
            if sentence.strip():
                self.skipped += 1
                log(f"  (not spoken: {sentence.strip()[:80]})")
            return
        self.queued += 1
        self.sentences.put_nowait((self.turn, text))

    def _brain_text(self, delta):
        if self.after_call:  # nothing is spoken any more; keep the last text block as the recap
            self.block += delta
            return
        if self._dropping():
            self.unheard += delta
            return
        self.pending += delta
        parts = SENTENCE_END.split(self.pending)
        for s in parts[:-1]:
            self._queue_sentence(s)
        self.pending = parts[-1]

    def _brain_block_end(self):
        if self.after_call:
            if self.block.strip():
                self.final_block = self.block
            self.block = ""
            return
        if not self._dropping():
            self._queue_sentence(self.pending)
        self.pending = ""

    def _brain_turn_end(self):
        # One reply can answer several messages (you said two things in a row), so a finished reply
        # means everything sent so far is answered; counting one-for-one left a phantom pending reply.
        # But only what the brain had read: a message sent while it was answering may get its own
        # reply next, and a barge-in must still be able to drop that one. The brain echoes each
        # message as it reads it, so a reply covers exactly the messages echoed before it.
        self.done = min(self.brain.consumed, self.sent) if self.brain.replays else self.sent
        if self.unheard and not self._dropping():
            log(f"  (not heard: {' '.join(self.unheard.split())[:240]})")
            self.unheard = ""
        if self.after_call:
            if self.block.strip():
                self.final_block = self.block
            self.after_done.set()
            return
        self.mixer.set_music(False)
        if not self._dropping():
            self._queue_sentence(self.pending)
            if self.skipped and not self.queued:
                self.sentences.put_nowait((self.turn, NOT_SPOKEN))
        self.pending = ""

    async def _speak(self):
        while True:
            turn, sentence = await self.sentences.get()
            if turn != self.turn:
                continue  # dropped by a barge-in
            if BYE in sentence:
                self.hangup_after_voice = True
                sentence = sentence.replace(BYE, "").strip()
            for marker, place in ((DESK, "desk"), (PHONE, "phone")):
                if marker in sentence:
                    sentence = sentence.replace(marker, "").strip()
                    if place != self.where:
                        self.move_after_voice = place
            if not sentence:
                continue
            t0 = time.monotonic()
            self.in_tts = sentence
            try:
                pcm = await asyncio.to_thread(tts, sentence)
            finally:
                self.in_tts = None
            if turn != self.turn:
                continue  # a barge-in while it was being voiced (_barge_in counted it as lost)
            if self.waiting_since is not None:
                log(f"  reply audio {time.monotonic() - self.waiting_since:.2f}s after you stopped "
                    f"(tts {1000 * (time.monotonic() - t0):.0f} ms)")
                self.waiting_since = None
            self.mixer.say(pcm)
            self.voiced.append((pcm, sentence))
            if self.reply_turn != turn:
                self.last_reply, self.reply_turn = [], turn
            self.last_reply.append(pcm)
            log(f"  claude: {sentence}")

    async def _watch(self):
        """Filler words, then music, while you wait; hang up after a goodbye."""
        filled = False
        while True:
            await asyncio.sleep(0.1)
            if self.waiting_since is None:
                filled = False
                if self.hangup_after_voice and not self.mixer.speaking and not self.hearing_speech:
                    await asyncio.sleep(0.5)
                    if self.hangup_after_voice and not self.hearing_speech:  # still nothing more from you
                        await self.hang_up()
                        return
                    continue
                if self.move_after_voice and not self.mixer.speaking:
                    place, self.move_after_voice = self.move_after_voice, None
                    await asyncio.sleep(0.3)
                    await self.move(place)
                    continue
                quiet = time.monotonic() - self.mixer.last_voice
                if (self.done < self.sent and not self.mixer.speaking and self.mixer.last_voice
                        and quiet > CONFIG["music_after_heads_up"] and self.where != "desk"):
                    self.mixer.set_music(True)  # still working after "Let me check."
                continue
            waited = time.monotonic() - self.waiting_since
            if waited > CONFIG["filler_after"] and not filled and not self.mixer.speaking:
                self.mixer.say(self.filler)
                filled = True
            if waited > CONFIG["music_after"] and self.where != "desk":  # music would shut the desk mic
                self.mixer.set_music(True)

    def _echo_risk(self):
        """At the desk the mic hears the speakers: is Omarchy talking, or did it just stop?"""
        return self.mixer.speaking or time.monotonic() - self.mixer.last_voice < CONFIG["desk_echo_tail"]

    async def _listen(self):
        desk = self.where == "desk"
        parec = await asyncio.create_subprocess_exec(
            "parec", f"--device={self._devices()[0]}", "--format=s16le", f"--rate={IN_RATE}",
            "--channels=1", "--raw", "--latency-msec=20", stdout=asyncio.subprocess.PIPE)
        try:
            async with websockets.connect(f"ws://{CONFIG['speech']}/listen", max_size=None) as ws:
                if self.hints:
                    await ws.send(json.dumps({"prompt": self.hints}))

                async def pump():
                    while chunk := await parec.stdout.read(640):
                        if desk and self._echo_risk():
                            chunk = bytes(len(chunk))  # silence instead of its own voice
                        await ws.send(chunk)
                pumper = asyncio.create_task(pump())
                held, release = "", None  # a sentence that trailed off, and the timer that sends it anyway

                async def send_held():
                    nonlocal held
                    await asyncio.sleep(CONFIG["unfinished_wait"])
                    text, held = held, ""
                    if text:
                        self._user_turn(text)
                try:
                    async for raw in ws:
                        ev = json.loads(raw)
                        if ev["type"] == "speech_start":
                            self.hearing_speech = True
                            if release:  # they carried on: wait for the rest of the sentence
                                release.cancel()
                                release = None
                            if self.mixer.speaking and not self.mixer.held:
                                self._barge_in()
                        elif ev["type"] == "speech_end":
                            self.hearing_speech = False
                            text = ev["text"]
                            if text:
                                log(f"  you: {text}  (stt {ev['stt_ms']} ms)")
                                if self.mixer.held:  # their "hello?": play the prepared opening
                                    self.mixer.held = False
                                    if len(text.split()) <= 3:
                                        continue  # just a greeting
                                if self.hangup_after_voice or self.move_after_voice:
                                    log("  (you kept talking: not hanging up or moving yet)")
                                    self.hangup_after_voice, self.move_after_voice = False, None
                            text, held = f"{held} {text}".strip(), ""
                            if not text:  # a cough, a bump: nothing was said
                                if self.cut_at and not self.mixer.speaking:  # ...but it cut an answer off
                                    cut, self.cut_at = self.cut_at, None
                                    log("  (only a noise: carrying on)")
                                    self._user_turn(f'[interrupted] A noise on the line cut you off while you were '
                                                    f'saying "{cut}", and the user heard nothing after that. '
                                                    "Carry on from there, briefly.")
                                continue
                            if UNFINISHED.search(text):
                                held = text
                                log("  (sounds unfinished: waiting a moment for the rest)")
                                release = asyncio.create_task(send_held())
                                continue
                            self._user_turn(text)
                finally:
                    pumper.cancel()
                    if release:
                        release.cancel()
        finally:
            parec.kill()


# --------------------------------------------------------------------------- calling you

SPOOL = Path(os.environ.get("VF_SPOOL", Path.home() / ".local/state/voice-foreman/events"))  # hook.py writes here
URGENT = {"permission_prompt", "elicitation_dialog"}  # a session is blocked on you


def sip_host(uri):
    """'sip:phone@100.64.0.2:5062' or '"X" <sip:100.64.0.2>' -> '100.64.0.2'."""
    m = re.search(r"sips?:(?:[^@>;]*@)?\[?([^\]:;>]+)", uri or "")
    return m.group(1).lower() if m else ""


def sip_user(uri):
    """'sip:vf-0a1b@100.64.0.1:5060' -> 'vf-0a1b'."""
    m = re.search(r"sips?:([^@>;:]+)@", uri or "")
    return m.group(1).lower() if m else ""


def line_user():
    """The line's secret user part, or "" while tools/configure-line.sh hasn't made one."""
    try:
        secret = Path(CONFIG["line_secret"]).read_text().strip()
    except OSError:
        return ""
    return "vf-" + secret if re.fullmatch(r"[0-9a-f]{32}", secret) else ""


def caller_allowed(uri):
    allowed = {h.strip().lower() for h in CONFIG["allowed_callers"].split(",") if h.strip()}
    return sip_host(uri) in (allowed or {sip_host(CONFIG["phone"])})


def refusal(ev):
    """Why an incoming call may not reach the brain, or None.

    The caller writes its own From, and baresip answers without SIP authentication, so the
    From host proves nothing: another account on this PC can reach the line's Tailscale address
    (the tailnet policy doesn't apply to the PC's own traffic) and claim to be the phone. What
    proves it is the line's address: baresip only takes calls to sip:vf-<secret>@this-PC, and
    the secret lives in your private config, where other accounts can't read it. A line still
    on a guessable address, from before configure-line.sh made a secret, takes no calls."""
    want = line_user()
    if not want:
        return "the line has no secret address yet: run tools/configure-line.sh, then restart vf-line"
    if sip_user(ev.get("accountaor", "")) != want:
        return "the line isn't on its secret address: run tools/configure-line.sh, then restart vf-line"
    if not caller_allowed(ev.get("peeruri", "")):
        return "a caller that isn't your phone"
    return None


def desktop_notify(title, body, critical=False):
    """A desktop notification, straight over D-Bus: notify-send would put the text on a command line,
    where any account on this machine could read it in the process list."""
    if not CONFIG["notify"]:
        return
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    service = DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                          interface="org.freedesktop.Notifications")
    hints = {"urgency": ("y", 2 if critical else 1)}
    try:
        with open_dbus_connection() as conn:
            conn.send_and_get_reply(new_method_call(service, "Notify", "susssasa{sv}i",
                                                    ("Omarchy", 0, "phone", title, body, [], hints, -1)), timeout=3)
    except Exception as e:  # no notification service: nothing to show it on
        log(f"notification not shown: {e.__class__.__name__}")


def shell(*cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def session_names():
    """session id -> session name, from `claude agents --json --all`."""
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True,
                             timeout=8).stdout
        return {a.get("sessionId"): a.get("name") for a in json.loads(out)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def classify(note):
    """'needs_you', 'finished', or None (not worth a call)."""
    event = note.get("hook_event_name")
    if event == "Stop":
        return None if note.get("background_tasks") else "finished"  # still working in the background
    if event == "Notification" and note.get("notification_type") in URGENT:
        return "needs_you"
    return None  # idle_prompt and the like: the Stop note already covered it


def missed_call_text(notes):
    """The text you get when you don't pick up."""
    parts = []
    for note in notes:
        where = Path(note.get("cwd", "")).name or "a session"
        if note["kind"] == "callback":
            parts.append(f"calling back about: {note.get('reason') or 'what we discussed'}")
        elif note["kind"] == "needs_you":
            parts.append(f"{where} is waiting for you")
        elif note["kind"] == "ping":
            parts.append(note.get("message") or "a job finished")
        else:
            last = " ".join((note.get("last_assistant_message") or "").split())
            parts.append(f"{where} finished: {last[:140]}")
    return (f"Omarchy, missed call: {'; '.join(parts)}. "
            f"I'll call again in {CONFIG['retry_after'] / 60:.0f} minutes.")


class Away:
    """You're away when the screen is locked or the PC has been idle for a while."""

    def __init__(self):
        self.idle_since = None
        self.locked = False
        self.dnd = False

    async def poll(self):
        while True:
            idle = await asyncio.to_thread(shell, "omarchy-shell", CONFIG["idle_ipc"], "idle") == "true"
            self.locked = await asyncio.to_thread(shell, "omarchy-shell", "lock", "isLocked") == "true"
            self.dnd = await asyncio.to_thread(shell, "omarchy-shell", "notifications", "isDnd") == "on"
            if not idle:
                self.idle_since = None
            elif self.idle_since is None:
                self.idle_since = time.monotonic() - 30  # the bar reports idle after 30 s
            await asyncio.sleep(15)

    @property
    def away(self):
        always = CONFIG["always_call"]
        if always is None:
            always = settings()["mode"] == "always"
        if always or self.locked:
            return True
        return self.idle_since is not None and time.monotonic() - self.idle_since >= CONFIG["away_after"]


class Dispatcher:
    """Reads the hook spool, decides when to call you, dials, and retries if you miss it."""

    def __init__(self, line):
        self.line = line
        self.away = Away()
        self.pending = []  # notes for the next call, latest per session
        self.due = None  # when to dial
        self.retry_at = 0.0
        self.ringing_since = None
        self.hung_up = False  # we gave up ringing; waiting for CALL_CLOSED
        self.call = None
        self.prepared = None  # the outgoing call, getting its opening ready while the phone rings
        self.calling_about = []  # the notes behind that call, kept in case you don't pick up
        self.quiet_until = 0.0  # after "call me back in N minutes": no calls before then

    def add(self, note):
        if note.get("hook_event_name") == "UserPromptSubmit":  # you answered it at the keyboard
            before = len(self.pending)
            self.pending = [p for p in self.pending if p.get("session_id") != note.get("session_id")]
            if len(self.pending) < before:
                log(f"you replied in {Path(note.get('cwd', '')).name}; not calling about it")
            if not self.pending:
                self.due = None
            return
        if note.get("hook_event_name") == "Desk":  # the bar's "Talk here" (vf-ctl desk)
            asyncio.create_task(self.desk(note.get("action") or "start"))
            return
        if note.get("hook_event_name") in ("Stop", "Notification") and note.get("session_id"):
            self.remember(note)
        calls_on = CONFIG["calls_override"] if CONFIG["calls_override"] is not None else settings()["calls"]
        if note.get("hook_event_name") == "Ping":  # vf-ping: a job you were waiting on finished
            self.ping(note, calls_on)
            return
        if not calls_on:
            log(f"{note.get('hook_event_name')} in {Path(note.get('cwd', '')).name or 'foremand'}: calls are off")
            return
        if note.get("hook_event_name") == "Callback":  # "call me back in 20 minutes" (the call_back tool, vf-callback)
            minutes = float(note["minutes"]) if note.get("minutes") is not None else 10.0  # 0 = now
            due = time.monotonic() + 60 * minutes
            note["kind"] = "callback"
            self.pending.append(note)
            self.quiet_until = max(self.quiet_until, due)  # you said you're busy until then
            self.due = min(self.due or float("inf"), due)
            log(f"callback in {minutes:.0f} min: {note.get('reason', '')}")
            return
        kind = classify(note)
        if kind is None:
            return
        who = note.get("session_id")
        where = Path(note.get("cwd", "")).name
        pref = session_pref(note)
        if pref == "mute":
            log(f"event: {kind} in {where}; muted in your preferences")
            return
        if pref == "text" and kind != "needs_you":
            last = " ".join((note.get("last_assistant_message") or "").split())
            asyncio.create_task(self.line.send("message", f"Omarchy: {where} finished: {last[:300]}"))
            log(f"event: {kind} in {where}; texted instead of calling (your preference)")
            return
        if self.call is None and not self.away.away and pref != "call":
            log(f"event: {kind} in {Path(note.get('cwd', '')).name}; you're at the PC, not calling")
            return
        note["kind"] = kind
        self.pending = [p for p in self.pending if p.get("session_id") != who] + [note]
        wait = CONFIG["urgent_wait"] if kind == "needs_you" else CONFIG["batch_wait"]
        self.due = min(self.due or float("inf"), time.monotonic() + wait)
        log(f"event: {kind} in {Path(note.get('cwd', '')).name}; calling in {self.due - time.monotonic():.0f}s")

    def ping(self, note, calls_on):
        """vf-ping. --text (or calls off): a text. --call: a call. Otherwise a call only when you're away;
        at the PC it's a desktop notification. Quiet hours hold a call like any other."""
        message = " ".join(str(note.get("message") or "A job you were waiting on finished.").split())[:300]
        how = note.get("how") or "auto"
        if how == "text" or not calls_on:
            asyncio.create_task(self.line.send("message", f"Omarchy: {message}"))
            log("ping: texted" + ("" if how == "text" else " (calls are off)"))
            return
        if how == "auto" and self.call is None and not self.away.away:
            asyncio.create_task(asyncio.to_thread(desktop_notify, "Omarchy", message))
            log("ping: you're at the PC, shown on the desktop")
            return
        note["kind"] = "ping"
        note["message"] = message
        self.pending.append(note)
        self.due = min(self.due or float("inf"), time.monotonic() + 2)
        log("ping: calling you about it")

    async def desk(self, action):
        """Start or stop a desk talk. Starting one during a phone call carries that call over."""
        call = self.call
        if action == "stop":
            if call and call.where == "desk":
                await call.hang_up()
            return
        if call and call.where == "phone":
            await call.move("desk")
            return
        if call or self.prepared or self.ringing_since:
            log("desk talk: not now, a call is starting or ringing")
            return
        call = Call(self.line, f"desk-{time.strftime('%H%M%S')}", "desk", "at the PC", self.take_agenda(), where="desk")
        self.on_established(call)
        await call.start()

    def end_desk(self, call):
        if self.call is call:
            self.call = None
        asyncio.create_task(call.finish())

    def ring_for(self, call):
        """Carry a desk talk over to the phone: dial it and hand this conversation to the call."""
        self.call, self.prepared = None, call
        self.ringing_since, self.hung_up, self.calling_about = time.monotonic(), False, []
        if not CONFIG["phone"]:
            log("can't carry on by phone: VF_PHONE isn't set")
            return
        log("calling you to carry on the conversation on the phone")
        asyncio.create_task(self.line.send("dial", CONFIG["phone"]))

    def take_agenda(self):
        """The pending notes as one paragraph for the brain, or None. Clears them."""
        if not self.pending:
            return None
        names = session_names()
        items = []
        for note in self.pending:
            who = names.get(note.get("session_id")) or Path(note.get("cwd", "")).name or "a session"
            if note["kind"] == "callback":
                items.append(f"You're calling the user back about: {note.get('reason') or 'what you discussed'}. "
                             "Get what you need, then finish the work after they hang up.")
            elif note["kind"] == "needs_you":
                items.append(f"Session {who} is waiting for the user: {note.get('message') or 'it needs a decision'}.")
            elif note["kind"] == "ping":
                items.append(f"Something the user was waiting on (vf-ping): {note.get('message')}")
            else:
                last = " ".join((note.get("last_assistant_message") or "").split())
                items.append(f"Session {who} finished. Its last message: \"{last[:700]}\"")
        waited = time.time() - min(n.get("at", time.time()) for n in self.pending) > 1800
        self.pending, self.due = [], None
        text = " ".join(f"({i}) {item}" for i, item in enumerate(items, 1))
        return text + (" These waited while the user had quiet hours or Do Not Disturb on." if waited else "")

    def remember(self, note):
        """Each session's latest message, so the brain can answer "what's X doing?" without digging."""
        try:
            sessions = json.loads(SESSIONS.read_text())
        except (OSError, ValueError):
            sessions = {}
        entry = sessions.setdefault(note["session_id"], {})
        entry.update(folder=note.get("cwd"), at=time.strftime("%H:%M", time.localtime(note.get("at", time.time()))))
        if note.get("hook_event_name") == "Stop":
            entry.update(state="finished", last_message=(note.get("last_assistant_message") or "")[-2000:])
        else:
            entry.update(state="waiting for the user", waiting_for=note.get("message"))
        try:
            SESSIONS.with_suffix(".tmp").write_text(json.dumps(sessions, indent=1))
            SESSIONS.with_suffix(".tmp").replace(SESSIONS)
        except OSError:
            pass

    def quiet(self):
        """Quiet hours or Do Not Disturb: keep the news for a digest call afterwards."""
        if os.environ.get("VF_IGNORE_QUIET") == "1":  # tests
            return False
        cfg = settings()
        return in_quiet_hours(cfg.get("quiet")) or (cfg.get("respect_dnd") and self.away.dnd)

    def write_status(self, now):
        def epoch(mono):
            return round(time.time() + mono - now) if mono and mono > now else None
        status = {"in_call": self.call is not None, "ringing": self.ringing_since is not None,
                  "desk": self.call is not None and self.call.where == "desk",
                  "quiet": self.quiet(),
                  "pending": len(self.pending), "next_call": epoch(max(self.due or 0, self.retry_at, self.quiet_until))
                  if self.pending else None, "callback": epoch(self.quiet_until), "updated": round(time.time())}
        try:
            STATUS.with_suffix(".tmp").write_text(json.dumps(status))
            STATUS.with_suffix(".tmp").replace(STATUS)
        except OSError:
            pass

    def claim_prepared(self):
        call, self.prepared = self.prepared, None
        return call

    def on_established(self, call):
        self.call, self.ringing_since, self.hung_up = call, None, False
        self.calling_about, self.quiet_until = [], 0.0

    async def on_closed(self, answered, reason):
        if answered:
            self.call = None
            return
        if self.ringing_since is None and not self.hung_up:
            return
        log(f"you didn't pick up ({reason or 'no answer'}); texting you, trying again in "
            f"{CONFIG['retry_after'] / 60:.0f} min")
        self.ringing_since, self.hung_up = None, False
        self.retry_at = time.monotonic() + CONFIG["retry_after"]
        if self.prepared:
            call = self.claim_prepared()
            await call.finish(answered=call.moved)  # a talk carried over from the desk still gets its recap
        if "timed out" in reason.lower() or "[110]" in reason:  # the phone didn't answer SIP at all
            out = await asyncio.to_thread(shell, "python3", "-I", str(PROJECT / "tools/probe.py"), timeout=90)
            log(f"phone unreachable; probe: {out}")
            if "repaired=yes" in out or out.startswith("reachable=yes"):
                self.retry_at = time.monotonic() + 30
        missed, self.calling_about = self.calling_about, []
        if missed:
            await self.line.send("message", missed_call_text(missed))
            self.pending = missed + self.pending
            self.due = time.monotonic()  # retry_at holds it back

    async def run(self):
        asyncio.create_task(self.away.poll())
        SPOOL.mkdir(parents=True, exist_ok=True)
        while True:
            for path in sorted(SPOOL.glob("*.json")):
                try:
                    note = json.loads(path.read_text())
                except (OSError, ValueError):
                    note = None
                path.unlink(missing_ok=True)
                if note:
                    self.add(note)
            if self.call and self.pending:
                self.call.update(self.take_agenda())
            now = time.monotonic()
            self.write_status(now)
            if self.ringing_since and not self.hung_up and now - self.ringing_since > CONFIG["ring_timeout"]:
                self.hung_up = True
                await self.line.send("hangup")
            elif (self.pending and CONFIG["phone"] and self.call is None and self.ringing_since is None and not self.hung_up
                  and now >= (self.due or float("inf")) and now >= self.retry_at and now >= self.quiet_until
                  and (not self.quiet() or all(p["kind"] == "callback" for p in self.pending))):
                self.ringing_since = now
                log(f"calling you about {len(self.pending)} thing(s)")
                self.calling_about = list(self.pending)
                self.prepared = Call(self.line, None, "outgoing", CONFIG["phone"], self.take_agenda())
                asyncio.create_task(self.prepared.prepare(hold=True))  # the opening is ready by "hello?"
                await self.line.send("dial", CONFIG["phone"])
            await asyncio.sleep(1)


# --------------------------------------------------------------------------- the line

class Line(linebus.Line):
    """The baresip line over D-Bus (see linebus.py). A command the line can't take (it's down or
    restarting) is logged by name, never with its text, and the call carries on."""

    async def send(self, command, params=""):
        try:
            return await super().send(command, params)
        except linebus.LineError as e:
            log(f"line: {command} didn't go through: {e}")
            return ""


async def main():
    global DISPATCHER
    for folder in {STATE, CALLS_LOG.parent, SPOOL.parent}:  # an older install made these readable by all
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    line = Line()
    await line.connect()
    dispatcher = DISPATCHER = Dispatcher(line)
    if os.environ.get("VF_DISPATCH", "1") != "0":  # tests turn it off to leave the real spool alone
        asyncio.create_task(dispatcher.run())
    log(f"foremand ready on the line (D-Bus), brain {CONFIG['model']} ({CONFIG['effort']})")
    calls, refused = {}, set()
    async for ev in line.events():
        kind, call_id = ev.get("type"), ev.get("id")
        if (kind in ("CALL_INCOMING", "CALL_ESTABLISHED") and ev.get("direction") == "incoming"
                and (call_id in refused or (why := refusal(ev)))):
            # Only the phone may call in. Hang up once per call: a second hangup after this call
            # is gone would land on whatever call is current.
            if call_id not in refused:
                refused.add(call_id)
                peer = ev.get("peeruri", "?")
                log(f"call {call_id}: refused, {why}: {peer}")
                await line.send("callfind", call_id)
                await line.send("hangup", "scode=403 reason=Forbidden")
                asyncio.create_task(asyncio.to_thread(desktop_notify, "Omarchy", f"Refused a call from {peer}: {why}", True))
            continue
        if kind == "CALL_CLOSED" and call_id in refused:
            refused.discard(call_id)
            continue
        if kind == "CALL_ESTABLISHED" and call_id not in calls:
            call = dispatcher.claim_prepared() if ev.get("direction") == "outgoing" else None
            if call:  # we placed it: the opening is already prepared
                calls[call_id] = call
                dispatcher.on_established(call)
                call.go_live(call_id)
            else:
                call = Call(line, call_id, ev.get("direction", "?"), ev.get("peeruri", "?"), dispatcher.take_agenda())
                calls[call_id] = call
                dispatcher.on_established(call)
                await call.start()
        elif kind in ("CALL_DTMF_START", "CALL_DTMF") and call_id in calls:
            calls[call_id].on_key(str(ev.get("param", "")).strip()[:1])
        elif kind == "CALL_CLOSED":
            call = calls.pop(call_id, None)
            if call and call.where == "desk":  # "move to my PC": only the phone leg ended
                log(f"call {call_id}: phone leg closed; the conversation carries on at the PC")
                continue
            if call:
                asyncio.create_task(call.finish())
            await dispatcher.on_closed(answered=call is not None, reason=ev.get("param", ""))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
