#!/usr/bin/env python3
"""Compose the waiting music: a calm lo-fi loop (electric piano chords, a soft bell arpeggio,
round bass, brushed hats), written to sounds/hold.wav as 24 kHz mono 16-bit.

Original and generated from scratch, so there's nothing to license. The loop is seamless:
note tails that run past the end wrap around to the start.
Usage: .venv/bin/python tools/make_hold_music.py [output.wav]
"""
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 24000
BPM = 78
BEAT = 60 / BPM
BARS_PER_CHORD = 2
# Fmaj7 - Em7 - Dm7 - Cmaj7 (+9 on the last), MIDI notes for the voicing and the bass root.
CHORDS = [
    ([53, 57, 60, 64], 41),
    ([52, 55, 59, 62], 40),
    ([50, 53, 57, 60], 38),
    ([48, 52, 55, 59, 62], 36),
]
LENGTH = len(CHORDS) * BARS_PER_CHORD * 4 * BEAT  # seconds
N = int(round(LENGTH * RATE))
rng = np.random.default_rng(7)


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def add(buf, start_s, sig):
    """Mix sig into the loop at start_s, wrapping the tail around to the beginning."""
    start = int(start_s * RATE) % N
    idx = (start + np.arange(len(sig))) % N
    np.add.at(buf, idx, sig)


def epiano(freq, dur, vel):
    t = np.arange(int(dur * RATE)) / RATE
    tone = sum(a * np.sin(2 * np.pi * freq * k * t) * np.exp(-t * d)
               for k, a, d in ((1, 1.0, 1.1), (2, 0.35, 2.5), (3, 0.12, 4.0), (4, 0.05, 6.0)))
    tremolo = 1 + 0.08 * np.sin(2 * np.pi * 4.5 * t)
    attack = np.minimum(1, t / 0.008)
    return vel * tone * tremolo * attack


def bell(freq, vel):
    t = np.arange(int(1.6 * RATE)) / RATE
    tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2.76 * t) * np.exp(-t * 6)
    return vel * tone * np.exp(-t * 3.2) * np.minimum(1, t / 0.004)


def bass(freq, dur, vel):
    t = np.arange(int(dur * RATE)) / RATE
    tone = np.sin(2 * np.pi * freq * t) + 0.2 * np.sin(2 * np.pi * freq * 2 * t)
    return vel * tone * np.exp(-t * 0.9) * np.minimum(1, t / 0.02) * np.minimum(1, (dur - t) / 0.05)


def hat(vel):
    n = int(0.06 * RATE)
    noise = rng.standard_normal(n)
    noise = np.diff(noise, prepend=0)  # crude high-pass: brushed, not hissy
    return vel * noise * np.exp(-np.arange(n) / RATE * 70)


def lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / RATE)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):  # one-pole; fine for a one-off render
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def compose():
    keys, bells, low, hats = (np.zeros(N) for _ in range(4))
    bar = 4 * BEAT
    for c, (notes, root) in enumerate(CHORDS):
        t0 = c * BARS_PER_CHORD * bar
        for b in range(BARS_PER_CHORD):
            tb = t0 + b * bar
            for i, n in enumerate(notes):  # strum the chord on beat 1, softer re-hit on beat 3 "and"
                add(keys, tb + i * 0.012, epiano(hz(n), 3.2, 0.22))
                add(keys, tb + 2.5 * BEAT + i * 0.01, epiano(hz(n), 2.0, 0.1))
            add(low, tb, bass(hz(root), 1.8 * BEAT, 0.55))
            add(low, tb + 2.5 * BEAT, bass(hz(root + 7), 1.2 * BEAT, 0.35))
            arp = notes[1:] + [notes[0] + 12]
            for k in range(8):  # gentle eighth-note bell line, an octave up, every other bar
                if b % 2 == 1 and k % 2 == 0:
                    continue
                add(bells, tb + k * BEAT / 2, bell(hz(arp[k % len(arp)] + 12), 0.06 + 0.02 * (k % 3 == 0)))
            for k in range(4):  # brushed hats on the off-beats, slightly swung
                add(hats, tb + k * BEAT + BEAT * 0.58, hat(0.05))
    mix = lowpass(keys + bells, 3200) + lowpass(low, 900) + hats
    mix += 0.004 * rng.standard_normal(N)  # a touch of vinyl air
    return mix / np.abs(mix).max() * 0.6


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "sounds/hold.wav"
    pcm = (compose() * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"wrote {out} ({len(pcm) / RATE:.1f} s loop)")


if __name__ == "__main__":
    main()
