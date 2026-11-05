"""Speech service for the voice foreman: STT, TTS and end-of-speech detection on the GPU.

Endpoints (compose publishes them on 127.0.0.1 only):
  GET  /health   what's loaded and which ONNX providers are in use
  GET  /voices   Kokoro voice names
  POST /stt      raw s16le 16 kHz mono -> {"text", "stt_ms", "audio_ms"}
  POST /tts      {"text", "voice"?, "speed"?, "lang"?} -> raw s16le mono (rate in X-Sample-Rate)
  WS   /listen   send raw s16le 16 kHz mono audio in any chunk size; JSON text messages
                 {"prompt": "..."} set Whisper's hint words, {"reset": true} drops buffered audio.
                 Receive {"type": "speech_start"} and
                 {"type": "speech_end", "text": "...", "audio_ms": n, "stt_ms": n}. Every
                 speech_start gets a speech_end; text is "" for sounds too short to be words.
"""
import asyncio
import collections
import json
import os
import time

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from faster_whisper import WhisperModel
from kokoro_onnx import Kokoro
from pydantic import BaseModel

STT_MODEL = os.environ.get("STT_MODEL", "large-v3-turbo")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE") or None  # None: detect per utterance
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
KOKORO_MODEL = "/models/kokoro/kokoro-v1.0.onnx"
KOKORO_VOICES = "/models/kokoro/voices-v1.0.bin"
VAD_MODEL = "/models/silero_vad.onnx"

RATE = 16000
WINDOW = 512  # Silero v5 window at 16 kHz: 32 ms
WINDOW_MS = WINDOW * 1000 // RATE
CONTEXT = 64  # samples of the previous window that Silero v5 expects in front
START_PROB, END_PROB = 0.5, 0.35
START_WINDOWS = 3  # ~96 ms of speech before speech_start
END_SILENCE_MS = int(os.environ.get("END_SILENCE_MS", "450"))
PREROLL_MS = int(os.environ.get("PREROLL_MS", "500"))  # audio kept from before speech_start
STT_BEAM = int(os.environ.get("STT_BEAM", "5"))  # 1 is fastest; 5 hears names and numbers better
MIN_SPEECH_MS = 250
GPU = ["CUDAExecutionProvider", "CPUExecutionProvider"]

whisper = WhisperModel(STT_MODEL, device="cuda", compute_type="float16")
kokoro_session = ort.InferenceSession(KOKORO_MODEL, providers=GPU)
kokoro = Kokoro.from_session(kokoro_session, KOKORO_VOICES)
vad_model = ort.InferenceSession(VAD_MODEL, providers=["CPUExecutionProvider"])
app = FastAPI()


def transcribe(audio, prompt=None):
    """audio: float32 at 16 kHz. Returns (text, milliseconds spent)."""
    t0 = time.monotonic()
    segments, _ = whisper.transcribe(audio, language=STT_LANGUAGE, beam_size=STT_BEAM, vad_filter=False,
                                     condition_on_previous_text=False, initial_prompt=prompt)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, round((time.monotonic() - t0) * 1000)


def synthesize(text, voice, speed, lang):
    """Returns (s16le bytes, sample rate, milliseconds spent)."""
    t0 = time.monotonic()
    samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return pcm, rate, round((time.monotonic() - t0) * 1000)


class Listener:
    """Streaming end-of-speech detection with Silero v5, with pre-roll and hysteresis."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.preroll = collections.deque(maxlen=PREROLL_MS // WINDOW_MS)
        self.speech = []
        self.in_speech = False
        self.voiced = 0
        self.silence_ms = 0

    def prob(self, window):
        x = np.concatenate([self.context, window])[None, :]
        out, self.state = vad_model.run(None, {"input": x, "state": self.state,
                                               "sr": np.array(RATE, dtype=np.int64)})
        self.context = window[-CONTEXT:]
        return float(out[0][0])

    def feed(self, pcm):
        """Feed s16le bytes; returns events: ("start",) and ("end", float32 audio)."""
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.pending = np.concatenate([self.pending, audio])
        events = []
        while len(self.pending) >= WINDOW:
            window, self.pending = self.pending[:WINDOW], self.pending[WINDOW:]
            p = self.prob(window)
            if not self.in_speech:
                self.preroll.append(window)
                self.voiced = self.voiced + 1 if p >= START_PROB else 0
                if self.voiced >= START_WINDOWS:
                    self.in_speech, self.speech, self.silence_ms = True, list(self.preroll), 0
                    events.append(("start",))
                continue
            self.speech.append(window)
            self.silence_ms = self.silence_ms + WINDOW_MS if p < END_PROB else 0
            if self.silence_ms >= END_SILENCE_MS:
                spoken_ms = len(self.speech) * WINDOW_MS - self.silence_ms
                if spoken_ms >= MIN_SPEECH_MS:
                    events.append(("end", np.concatenate(self.speech)))
                else:  # too short for words (a cough, a bump): still close the speech_start
                    events.append(("end", None))
                self.in_speech, self.speech, self.voiced = False, [], 0
                self.preroll.clear()
        return events


@app.on_event("startup")
def warm_up():
    transcribe(np.zeros(RATE, dtype=np.float32))
    synthesize("Ready.", TTS_VOICE, 1.0, "en-us")


@app.get("/health")
def health():
    return {"ok": True, "stt_model": STT_MODEL, "stt_language": STT_LANGUAGE or "auto",
            "tts_voice": TTS_VOICE, "tts_providers": kokoro_session.get_providers(),
            "end_silence_ms": END_SILENCE_MS, "preroll_ms": PREROLL_MS, "stt_beam": STT_BEAM}


@app.get("/voices")
def voices():
    return sorted(kokoro.get_voices())


@app.post("/stt")
async def stt(request: Request):
    audio = np.frombuffer(await request.body(), dtype="<i2").astype(np.float32) / 32768.0
    text, ms = await asyncio.to_thread(transcribe, audio)
    return {"text": text, "stt_ms": ms, "audio_ms": len(audio) * 1000 // RATE}


class TtsRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0
    lang: str = "en-us"


@app.post("/tts")
async def tts(req: TtsRequest):
    pcm, rate, ms = await asyncio.to_thread(synthesize, req.text, req.voice or TTS_VOICE, req.speed, req.lang)
    return Response(pcm, media_type="application/octet-stream",
                    headers={"X-Sample-Rate": str(rate), "X-TTS-Ms": str(ms)})


@app.websocket("/listen")
async def listen(ws: WebSocket):
    await ws.accept()
    listener, prompt = Listener(), None
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                cfg = json.loads(msg["text"])
                prompt = cfg.get("prompt", prompt)
                if cfg.get("reset"):
                    listener.reset()
                continue
            for event in listener.feed(msg.get("bytes") or b""):
                if event[0] == "start":
                    await ws.send_json({"type": "speech_start"})
                    continue
                audio = event[1]
                if audio is None:
                    await ws.send_json({"type": "speech_end", "text": "", "audio_ms": 0, "stt_ms": 0})
                    continue
                text, ms = await asyncio.to_thread(transcribe, audio, prompt)
                await ws.send_json({"type": "speech_end", "text": text,
                                    "audio_ms": len(audio) * 1000 // RATE, "stt_ms": ms})
    except WebSocketDisconnect:
        pass
