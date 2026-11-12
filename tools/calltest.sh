#!/bin/sh
# End-to-end "it calls you" test, no phone needed: a fake phone (baresip on port 5074,
# answers by itself, records what it hears) stands in for yours. A synthetic "session finished"
# note goes into a private spool; foremand on the dev line should dial the fake phone and report it.
# Needs the speech service and the vf_rx/vf_tx sinks. Usage: tools/calltest.sh [ping]
# With "ping", a `vf-ping --call` stands in for the session: a build that finished.
set -e
# The dev line takes the same D-Bus name as your real line, so each test runs on a private bus of its own.
[ -n "$VF_TEST_BUS" ] || VF_TEST_BUS=1 exec dbus-run-session -- sh "$0" "$@"
P=$(cd "$(dirname "$0")/.." && pwd)
# the speech service listens on a socket in your runtime folder, not on a port
SPEECH=${VF_SPEECH_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voice-foreman/speech/speech.sock}
# This PC's Tailscale address, as tools/configure-line.sh wrote it into the dev line.
ADDR=$(sed -n -E 's/^sip_listen\s+([^:]+):.*/\1/p' "$P/line/baresip-dev/config")
[ -n "$ADDR" ] || { echo "run tools/configure-line.sh first" >&2; exit 1; }
T=$HOME/.cache/vf-tmp/calltest
rm -rf "$T" && mkdir -p -m 700 "$T" && mkdir -p "$T/phone" "$T/spool"
own_bus() {  # own_bus <bus file> <command...>: a fake gets a private D-Bus of its own (the dev line holds the test's)
  f=$1; shift
  dbus-run-session -- sh -c 'echo "$DBUS_SESSION_BUS_ADDRESS" > "$0"; exec "$@"' "$f" "$@"
}
hangup_on() {  # hangup_on <bus file>: that fake hangs up, as you would on the phone
  DBUS_SESSION_BUS_ADDRESS=$(cat "$1") busctl --user call com.github.Baresip /baresip com.github.Baresip invoke s hangup >/dev/null
}

# 1. A fake phone that answers by itself and stays quiet (40 s of silence as its microphone).
ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=48000:cl=mono -t 40 "$T/silence.wav"
cp "$P/line/baresip-dev/config" "$T/phone/"
sed -i -E \
  -e "s|^sip_listen\s.*|sip_listen\t\t$ADDR:5074|" \
  -e 's|^rtp_ports\s.*|rtp_ports\t\t43000-43019|' \
  -e "s|^audio_source\s.*|audio_source\t\taufile,$T/silence.wav|" \
  -e "s|^audio_player\s.*|audio_player\t\taufile,$T/heard.wav|" \
  -e "s|^audio_alert\s.*|audio_alert\t\taufile,$T/alert.wav|" "$T/phone/config"
echo "\"Phone\" <sip:phone@$ADDR:5074>;regint=0;answermode=auto;audio_codecs=opus/48000/2;mediaenc=srtp-mand" > "$T/phone/accounts"
touch "$T/phone/contacts"
own_bus "$T/phone.bus" baresip -f "$T/phone" -T -c -t 90 > "$T/phone.log" 2>&1 &
PHONE=$!
# Texts reach the fake phone as D-Bus signals on its bus: record them.
sleep 0.5
DBUS_SESSION_BUS_ADDRESS=$(cat "$T/phone.bus") busctl --user --json=short monitor com.github.Baresip > "$T/phone-bus.jsonl" 2>/dev/null &
MONITOR=$!

# 2. Dev line + foremand that always counts you as away and calls 2 s after a finish.
baresip -f "$P/line/baresip-dev" -T -c > "$T/line.log" 2>&1 &
LINE=$!
sleep 1
VF_STATE="$T/state" VF_NOTIFY=0 VF_CALLS=1 VF_IGNORE_QUIET=1 VF_CALLS_LOG="$T/calls.jsonl" VF_SPOOL="$T/spool" VF_ALWAYS_CALL=1 VF_BATCH_WAIT=2 \
  VF_PHONE=sip:phone@$ADDR:5074 \
  "$P/.venv/bin/python" "$P/foremand/foremand.py" > "$T/foremand.log" 2>&1 &
FOREMAND=$!
sleep 2

if [ "$1" = ping ]; then
  # 3. A build you were waiting on finishes.
  VF_SPOOL="$T/spool" "$P/bin/vf-ping" --call -- sh -c "sleep 1; exit 0"
else
  # 3. A session "finishes".
  cat > "$T/spool/1-Stop.json" <<'EOF'
{"hook_event_name": "Stop", "session_id": "calltest", "cwd": "/home/me/projects/shop-api",
   "last_assistant_message": "Fixed both review comments on item 552 and all 48 tests pass. The pull request is ready for you to merge."}
EOF
fi

sleep 30
hangup_on "$T/phone.bus"  # hang up like you would; the recap follows
sleep 30
pkill -P $PHONE 2>/dev/null; kill $MONITOR $FOREMAND $LINE $PHONE 2>/dev/null || true

echo "== foremand log"; cat "$T/foremand.log"
if [ -s "$T/heard.wav" ]; then
  ffmpeg -loglevel error -y -i "$T/heard.wav" -f s16le -ar 16000 -ac 1 "$T/heard.raw"
  echo "== what the phone heard (transcribed)"
  curl -sf --unix-socket "$SPEECH" -X POST http://speech/stt --data-binary @"$T/heard.raw"; echo
else
  echo "== the phone recorded nothing; see $T/phone.log and $T/line.log"
fi
echo "== text the phone received"
python3 -I -c 'import json, sys
for line in open(sys.argv[1]):
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("member") == "message":
        print(m["payload"]["data"][3])' "$T/phone-bus.jsonl"
