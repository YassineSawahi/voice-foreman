"""Barge-in and listening logic, offline: no phone, no brain process, no speech server.

Replays two kinds of moment that went wrong on calls, with a fake brain and a fake speech server:
  - a reply to an earlier "Thank you." kept playing after you talked over it ("...ytime.")
  - "Are my sessions still running?" -> "Let me check." -> you talked -> the answer was dropped and
    the brain, not knowing, just said "You're welcome"
and checks the trailing-off hold ("a report about the...") and picking up after a noise.

    .venv/bin/python tools/bargetest.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("VF_STATE", tempfile.mkdtemp(prefix="vf-bargetest-"))  # never the live log
os.environ["VF_UNFINISHED_WAIT"] = "0.3"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "foremand"))
import numpy as np  # noqa: E402

import foremand as fm  # noqa: E402

failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def new_call():
    call = fm.Call(line=None, call_id="test", direction="incoming", peer="sip:test")
    call.sent_to_brain = []
    call.brain.send = call.sent_to_brain.append
    return call


def brain_reads(call, n=1):
    """The brain echoes a message it has read."""
    call.brain.consumed += n
    call.brain.replays = True


def voice_queue(call):
    """What _speak does: every queued sentence of the current turn goes to the mixer."""
    while not call.sentences.empty():
        turn, sentence = call.sentences.get_nowait()
        if turn != call.turn:
            continue
        pcm = np.zeros(800, dtype=np.int16)
        call.mixer.say(pcm)
        call.voiced.append((pcm, sentence))


def stale_reply():
    call = new_call()
    call._user_turn("Hang on, someone's at the door.")
    call._user_turn("Thank you.")
    brain_reads(call)  # reads the first; the second waits for its own reply
    call._brain_text("No problem, take your time. ")
    call._brain_text("Do you need anything from me? ")
    call._brain_turn_end()
    check(call.done == 1 and call.sent == 2, "a reply covers only the messages the brain had read")
    voice_queue(call)
    brain_reads(call)  # now the "Thank you."
    call._brain_text("An")
    call._barge_in()  # you talk over it
    call._brain_text("ytime. Talk soon, bye for now. [bye]")
    call._brain_turn_end()
    voice_queue(call)
    spoken = [s for _, s in call.voiced]
    check(not any("ytime" in s or "Talk soon" in s for s in spoken), "the stale reply to \"Thank you.\" is not played")
    check(call.cut_at == "No problem, take your time.",
          "the brain will be told where it was cut off")
    call._user_turn("Are my sessions still running?")
    last = call.sent_to_brain[-1]
    check(last.startswith("[interrupted]") and last.endswith("Are my sessions still running?"),
          "the next message carries the [interrupted] note")
    check(call.cut_at is None, "the note is sent once")


def lost_answer():
    call = new_call()
    call._user_turn("Are my sessions still running?")
    brain_reads(call)
    call._brain_text("Let me check.")
    call._brain_block_end()  # right before the tool call
    voice_queue(call)
    call._barge_in()  # "Okay, thanks." over "Let me check."
    call._brain_text("Docs and one billing session are busy; work is idle.")
    call._brain_turn_end()
    voice_queue(call)
    check(not any("busy" in s for _, s in call.voiced), "the answer after the barge-in is dropped")
    call._user_turn("Okay, thanks.")
    note = call.sent_to_brain[-1]
    check(note.startswith("[interrupted]") and '"Let me check."' in note,
          "the brain learns the user never heard the answer")


def keypad_quiet():
    call = new_call()
    call._user_turn("Tell me everything.")
    brain_reads(call)
    call._brain_text("First thing. Second thing. ")
    voice_queue(call)
    call.on_key("0")
    call._user_turn("Okay.")
    check(not call.sent_to_brain[-1].startswith("[interrupted]"), "keypad 0 (quiet please) adds no note")


def old_claude():
    call = new_call()  # a claude that never echoes messages: fall back to counting replies
    call._user_turn("one")
    call._user_turn("two")
    call._brain_text("Both. ")
    call._brain_turn_end()
    check(call.done == 2, "without echoes a reply still counts as answering everything sent")


class FakeWS:
    """The speech server's /listen: yields events; a callable item runs instead (what happens in between)."""

    def __init__(self, items, gap=0.0):
        self.items, self.gap = items, gap

    async def send(self, _):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self.items:
            if callable(item):
                item()
                continue
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
                continue
            yield json.dumps(item)
        await asyncio.sleep(0.6)  # longer than VF_UNFINISHED_WAIT: a held sentence gets sent


class FakeProc:
    class stdout:
        @staticmethod
        async def read(_):
            await asyncio.sleep(3600)

    def kill(self):
        pass


async def listen(call, items):
    async def fake_exec(*_, **__):
        return FakeProc()
    fm.speech_socket = lambda: "speech.sock"
    fm.websockets.unix_connect = lambda *_, **__: FakeWS(items)
    real_exec, fm.asyncio.create_subprocess_exec = fm.asyncio.create_subprocess_exec, fake_exec
    try:
        await call._listen()
    finally:
        fm.asyncio.create_subprocess_exec = real_exec


def said(text):
    return [{"type": "speech_start"}, {"type": "speech_end", "text": text, "stt_ms": 1}]


async def held_sentence():
    call = new_call()
    await listen(call, said("Text me a report about the...") + [0.1] + said("billing session."))
    check(call.sent_to_brain == ["Text me a report about the... billing session."],
          "a sentence that trails off waits for the rest and goes as one")
    call = new_call()
    await listen(call, said("Text me a report about the..."))
    check(call.sent_to_brain == ["Text me a report about the..."], "...and goes alone if nothing follows")
    call = new_call()
    await listen(call, said("Are my sessions still running?"))
    check(call.sent_to_brain == ["Are my sessions still running?"], "a finished sentence goes straight away")


async def noise():
    call = new_call()
    call._user_turn("Are my sessions still running?")
    brain_reads(call)
    call._brain_text("Let me check.")
    call._brain_block_end()
    voice_queue(call)
    await listen(call, [{"type": "speech_start"},  # a bump on the phone, while "Let me check." plays
                        call.mixer.voice.clear,  # the fade-out (the mixer task does it on a real call)
                        {"type": "speech_end", "text": "", "stt_ms": 0}])
    check(call.hearing_speech is False, "a too-short sound still closes speech_start")
    check(call.sent_to_brain[-1].startswith("[interrupted] A noise"), "after a noise it carries on where it was cut")


def main():
    stale_reply()
    lost_answer()
    keypad_quiet()
    old_claude()
    asyncio.run(held_sentence())
    asyncio.run(noise())
    print("PASS" if not failures else f"FAILED: {len(failures)}")
    sys.exit(1 if failures else 0)


main()
