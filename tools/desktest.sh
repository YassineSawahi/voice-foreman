#!/bin/sh
# Desk-mode test, no phone and no real mic or speakers: two throwaway PipeWire sinks stand in for the
# PC's microphone (speech is played into it) and speakers (recorded). Two scenarios, each with a fresh
# dev line and foremand:
#   A  desk talk from the bar, a question, then "call me": the talk carries over to a fake phone
#   B  a fake caller says "move this to my PC": the talk carries over to the desk, then goodbye
# Needs the speech service and the vf_rx/vf_tx sinks. Usage: tools/desktest.sh [A|B]
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
T=$HOME/.cache/vf-tmp/desktest
WHICH=${1:-AB}
rm -rf "$T" && mkdir -p -m 700 "$T"

SINK0=$(pactl get-default-sink); SOURCE0=$(pactl get-default-source)
MIC=$(pactl load-module module-null-sink sink_name=vf_test_mic sink_properties=device.description=vf-test-mic)
SPK=$(pactl load-module module-null-sink sink_name=vf_test_spk sink_properties=device.description=vf-test-speakers)
PIDS=""
cleanup() {
  for p in $PIDS; do pkill -P "$p" 2>/dev/null; done; kill $PIDS 2>/dev/null || true
  pactl unload-module "$MIC" 2>/dev/null; pactl unload-module "$SPK" 2>/dev/null
  pactl set-default-sink "$SINK0"; pactl set-default-source "$SOURCE0"  # never leave your audio moved
}
trap cleanup EXIT
own_bus() {  # own_bus <bus file> <command...>: a fake gets a private D-Bus of its own (the dev line holds the test's)
  f=$1; shift
  dbus-run-session -- sh -c 'echo "$DBUS_SESSION_BUS_ADDRESS" > "$0"; exec "$@"' "$f" "$@"
}
hangup_on() {  # hangup_on <bus file>: that fake hangs up, as you would on the phone
  DBUS_SESSION_BUS_ADDRESS=$(cat "$1") busctl --user call com.github.Baresip /baresip com.github.Baresip invoke s hangup >/dev/null
}

tts() { curl -sf --unix-socket "$SPEECH" -X POST http://speech/tts -H 'Content-Type: application/json' -d "{\"text\": \"$1\"}" -o "$2"; }
say() {  # speak into the fake desk microphone
  tts "$1" "$S/say.raw"
  echo "$(date +%T.%3N) >> desk mic: $1" >> "$S/script.log"
  pacat --playback --device=vf_test_mic --format=s16le --rate=24000 --channels=1 --raw < "$S/say.raw"
}
count() { grep -cE "$1" "$S/foremand.log" 2>/dev/null || true; }
wait_for() {  # wait_for <regex> <at least N matches> <seconds>
  for i in $(seq 1 "$3"); do [ "$(count "$1")" -ge "$2" ] && return 0; sleep 1; done
  echo "!! timed out waiting for: $1" | tee -a "$S/script.log"; return 0
}
settle() {  # wait until Omarchy has said nothing new for 3 s
  last=-1
  while :; do n=$(count "claude:|moving|carry on|phone leg|recap texted|recap kept|no recap"); [ "$n" = "$last" ] && break; last=$n; sleep 3; done
}
start_line_and_foremand() {  # $1 extra env
  baresip -f "$P/line/baresip-dev" -T -c > "$S/line.log" 2>&1 &
  PIDS="$PIDS $!"
  sleep 1
  env VF_STATE="$S/state" VF_SPOOL="$S/spool" VF_CALLS_LOG="$S/calls.jsonl" VF_NOTIFY=0 VF_CALLS=1 \
    VF_IGNORE_QUIET=1 VF_PHONE=sip:phone@$ADDR:5074 \
    VF_ALLOWED_CALLERS=$ADDR VF_DESK_SOURCE=vf_test_mic.monitor VF_DESK_SINK=vf_test_spk $1 \
    "$P/.venv/bin/python" "$P/foremand/foremand.py" > "$S/foremand.log" 2>&1 &
  PIDS="$PIDS $!"
  sleep 2
}
fake_baresip() {  # fake_baresip <dir> <port> <rtp base> <mic wav> <account line> [extra args]
  mkdir -p "$1"
  cp "$P/line/baresip-dev/config" "$1/"
  sed -i -E \
    -e "s|^sip_listen\s.*|sip_listen\t\t$ADDR:$2|" \
    -e "s|^rtp_ports\s.*|rtp_ports\t\t$3-$(($3 + 19))|" \
    -e "s|^audio_source\s.*|audio_source\t\taufile,$4|" \
    -e "s|^audio_player\s.*|audio_player\t\taufile,$1/heard.wav|" \
    -e "s|^audio_alert\s.*|audio_alert\t\taufile,$1/alert.wav|" "$1/config"
  echo "$5" > "$1/accounts"; touch "$1/contacts"
}
desk_heard() {
  parec --device=vf_test_spk.monitor --format=s16le --rate=16000 --channels=1 --raw > "$S/desk-heard.raw" &
  PIDS="$PIDS $!"
}
report() {
  echo; echo "===== scenario $1"; cat "$S/script.log" 2>/dev/null
  echo "== foremand"; grep -vE "opening:" "$S/foremand.log" | cut -c1-170
  echo "== heard on the desk speakers"; curl -sf --unix-socket "$SPEECH" -X POST http://speech/stt --data-binary @"$S/desk-heard.raw"; echo
  for f in "$S"/*/heard.wav; do
    [ -s "$f" ] || continue
    ffmpeg -loglevel error -y -i "$f" -f s16le -ar 16000 -ac 1 "$f.raw"
    echo "== heard by $(basename "$(dirname "$f")")"; curl -sf --unix-socket "$SPEECH" -X POST http://speech/stt --data-binary @"$f.raw"; echo
  done
}

case "$WHICH" in *A*)
  S=$T/A; mkdir -p "$S/spool"; PIDS=""
  # the fake phone says "Hello?" 1.5 s after it picks up
  tts "Hello?" "$S/hello.raw"
  ffmpeg -loglevel error -y -f s16le -ar 24000 -ac 1 -i "$S/hello.raw" -af "adelay=1500,apad=pad_dur=60" -ar 48000 "$S/phone-mic.wav"
  fake_baresip "$S/phone" 5074 43000 "$S/phone-mic.wav" "\"Phone\" <sip:phone@$ADDR:5074>;regint=0;answermode=auto;audio_codecs=opus/48000/2;mediaenc=srtp-mand"
  own_bus "$S/phone.bus" baresip -f "$S/phone" -T -c -t 200 > "$S/phone.log" 2>&1 &
  PIDS="$PIDS $!"
  start_line_and_foremand ""
  desk_heard
  echo '{"hook_event_name": "Desk", "action": "start"}' > "$S/spool/1-Desk.json"   # the bar's "Talk here"
  wait_for "claude:" 1 60; settle
  say "What is two plus two?"
  wait_for "you: .*(two|2)" 1 20; wait_for "claude:" 2 40; settle
  say "Thanks. I'm heading out, call me on my phone."
  wait_for "calling you to carry on" 1 40; wait_for "call [0-9a-f]+: desk" 1 30
  sleep 4; settle
  hangup_on "$S/phone.bus"   # you hang up the phone
  wait_for "recap texted|recap kept|no recap" 1 150
  report A
  for p in $PIDS; do pkill -P "$p" 2>/dev/null; done; kill $PIDS 2>/dev/null || true; sleep 1
;; esac

case "$WHICH" in *B*)
  S=$T/B; mkdir -p "$S/spool"; PIDS=""
  # the caller asks to move 6 s in (after the greeting)
  tts "Please move this call to my PC." "$S/ask.raw"
  ffmpeg -loglevel error -y -f s16le -ar 24000 -ac 1 -i "$S/ask.raw" -af "adelay=6000,apad=pad_dur=40" -ar 48000 "$S/caller-mic.wav"
  fake_baresip "$S/caller" 5072 42000 "$S/caller-mic.wav" "\"Caller\" <sip:caller@$ADDR:5072>;regint=0;audio_codecs=opus/48000/2;mediaenc=srtp-mand"
  start_line_and_foremand ""
  desk_heard
  # dialled from its contacts: the secret address is never on a command line, which other accounts can read
  echo "\"Omarchy\" <sip:$LINE_USER@$ADDR:5070>" > "$S/caller/contacts"
  own_bus "$S/caller.bus" baresip -f "$S/caller" -T -c -t 60 -e "/contact_next" -e "/dialcontact" > "$S/caller.log" 2>&1 &
  PIDS="$PIDS $!"
  wait_for "phone leg closed" 1 60; settle
  say "What day is it today?"
  wait_for "you: .*day" 1 20; wait_for "claude:" 3 40; settle
  say "Great, thanks. Bye."
  wait_for "recap texted|recap kept|no recap" 1 150
  report B
;; esac
