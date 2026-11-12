#!/bin/sh
# End-to-end talk test, no phone needed: a fake caller (baresip on port 5072) calls the
# dev line, asks a spoken question, records what Claude says back, and the speech service
# transcribes the recording. Needs the speech service up (systemctl --user start vf-speech) and the
# vf_rx/vf_tx sinks. Usage: tools/talktest.sh ["question text"]
set -e
# The dev line takes the same D-Bus name as your real line, so each test runs on a private bus of its own.
[ -n "$VF_TEST_BUS" ] || VF_TEST_BUS=1 exec dbus-run-session -- sh "$0" "$@"
P=$(cd "$(dirname "$0")/.." && pwd)
# the speech service listens on a socket in your runtime folder, not on a port
SPEECH=${VF_SPEECH_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voice-foreman/speech/speech.sock}
# This PC's Tailscale address, as tools/configure-line.sh wrote it into the dev line.
ADDR=$(sed -n -E 's/^sip_listen\s+([^:]+):.*/\1/p' "$P/line/baresip-dev/config")
[ -n "$ADDR" ] || { echo "run tools/configure-line.sh first" >&2; exit 1; }
# The dev line's secret address: it takes calls to no other.
LINE_USER=$(sed -n -E 's/^"Omarchy" <sip:(vf-[0-9a-f]{32})@.*/\1/p' "$P/line/baresip-dev/accounts")
[ -n "$LINE_USER" ] || { echo "run tools/configure-line.sh again: the dev line has no secret address" >&2; exit 1; }
T=$HOME/.cache/vf-tmp/talktest
QUESTION=${1:-"Hi Claude. What is two plus two?"}
rm -rf "$T" && mkdir -p -m 700 "$T" && mkdir -p "$T/caller"
own_bus() {  # own_bus <bus file> <command...>: a fake gets a private D-Bus of its own (the dev line holds the test's)
  f=$1; shift
  dbus-run-session -- sh -c 'echo "$DBUS_SESSION_BUS_ADDRESS" > "$0"; exec "$@"' "$f" "$@"
}
hangup_on() {  # hangup_on <bus file>: that fake hangs up, as you would on the phone
  DBUS_SESSION_BUS_ADDRESS=$(cat "$1") busctl --user call com.github.Baresip /baresip com.github.Baresip invoke s hangup >/dev/null
}

# 1. The question as audio: 6 s of silence (Claude greets first), the question, then room to answer.
if [ -n "$TALK_WAV" ]; then cp "$TALK_WAV" "$T/question.wav"  # a prepared script of what the caller says
else
curl -sf --unix-socket "$SPEECH" -X POST http://speech/tts -H 'Content-Type: application/json' \
  -d "{\"text\": \"$QUESTION\"}" -o "$T/q.raw"
ffmpeg -loglevel error -y -f s16le -ar 24000 -ac 1 -i "$T/q.raw" \
  -af "adelay=6000,apad=pad_dur=14" -ar 48000 "$T/question.wav"
fi

# 2. A throwaway caller: plays question.wav, records what it hears into answer.wav.
cp "$P/line/baresip-dev/config" "$T/caller/"
sed -i -E \
  -e "s|^sip_listen\s.*|sip_listen\t\t$ADDR:5072|" \
  -e 's|^rtp_ports\s.*|rtp_ports\t\t42000-42019|' \
  -e "s|^audio_source\s.*|audio_source\t\taufile,$T/question.wav|" \
  -e "s|^audio_player\s.*|audio_player\t\taufile,$T/answer.wav|" \
  -e "s|^audio_alert\s.*|audio_alert\t\taufile,$T/alert.wav|" "$T/caller/config"
echo "\"Caller\" <sip:caller@$ADDR:5072>;regint=0;audio_codecs=opus/48000/2;mediaenc=srtp-mand" > "$T/caller/accounts"
# It dials the line from its contacts: the secret address is never on a command line, which other accounts can read.
echo "\"Omarchy\" <sip:$LINE_USER@$ADDR:5070>" > "$T/caller/contacts"

# 3. Dev line + foremand, then the call.
baresip -f "$P/line/baresip-dev" -T -c > "$T/line.log" 2>&1 &
LINE=$!
sleep 1
VF_ALLOWED_CALLERS=${VF_ALLOWED_CALLERS:-$ADDR} VF_DISPATCH=0 VF_SPOOL="$T/spool" VF_STATE="$T/state" VF_NOTIFY=0 VF_CALLS=1 VF_IGNORE_QUIET=1 VF_CALLS_LOG="$T/calls.jsonl" "$P/.venv/bin/python" "$P/foremand/foremand.py" > "$T/foremand.log" 2>&1 &
FOREMAND=$!
sleep 1
own_bus "$T/caller.bus" baresip -f "$T/caller" -T -c -t 30 -e "/contact_next" -e "/dialcontact" > "$T/caller.log" 2>&1 || true
# after the hang-up Omarchy may still be working on what it promised; wait for the recap (up to 3 min)
for i in $(seq 1 180); do grep -qE "recap texted|recap skipped|no recap|ran out of time|refused a caller" "$T/foremand.log" && break; sleep 1; done
kill $FOREMAND $LINE 2>/dev/null || true

# 4. What did Claude say?
echo "== foremand log"; cat "$T/foremand.log"
if [ -s "$T/answer.wav" ]; then
  ffmpeg -loglevel error -y -i "$T/answer.wav" -f s16le -ar 16000 -ac 1 "$T/answer.raw"
  echo "== what the caller heard (transcribed)"
  curl -sf --unix-socket "$SPEECH" -X POST http://speech/stt --data-binary @"$T/answer.raw"; echo
else
  echo "== the caller recorded nothing; see $T/caller.log and $T/line.log"
fi
