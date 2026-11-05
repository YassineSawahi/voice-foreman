#!/bin/sh
# End-to-end "it calls you" test, no phone needed: a fake phone (baresip on port 5074,
# answers by itself, records what it hears) stands in for yours. A synthetic "session finished"
# note goes into a private spool; foremand on the dev line should dial the fake phone and report it.
# Needs the speech service and the vf_rx/vf_tx sinks. Usage: tools/calltest.sh
set -e
P=$(cd "$(dirname "$0")/.." && pwd)
# This PC's Tailscale address, as tools/configure-line.sh wrote it into the dev line.
ADDR=$(sed -n -E 's/^sip_listen\s+([^:]+):.*/\1/p' "$P/line/baresip-dev/config")
[ -n "$ADDR" ] || { echo "run tools/configure-line.sh first" >&2; exit 1; }
T=$HOME/.cache/vf-tmp/calltest
rm -rf "$T" && mkdir -p "$T/phone" "$T/spool"

# 1. A fake phone that answers by itself and stays quiet (40 s of silence as its microphone).
ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=48000:cl=mono -t 40 "$T/silence.wav"
cp "$P/line/baresip-dev/config" "$T/phone/"
sed -i -E \
  -e "s|^sip_listen\s.*|sip_listen\t\t$ADDR:5074|" \
  -e 's|^rtp_ports\s.*|rtp_ports\t\t43000-43019|' \
  -e "s|^audio_source\s.*|audio_source\t\taufile,$T/silence.wav|" \
  -e "s|^audio_player\s.*|audio_player\t\taufile,$T/heard.wav|" \
  -e "s|^audio_alert\s.*|audio_alert\t\taufile,$T/alert.wav|" \
  -e 's|^module_app\s+ctrl_tcp\.so|#module_app\t\tctrl_tcp.so|' "$T/phone/config"
echo "\"Phone\" <sip:phone@$ADDR:5074>;regint=0;answermode=auto;audio_codecs=opus/48000/2" > "$T/phone/accounts"
touch "$T/phone/contacts"
baresip -f "$T/phone" -T -c -t 90 > "$T/phone.log" 2>&1 &
PHONE=$!

# 2. Dev line + foremand that always counts you as away and calls 2 s after a finish.
baresip -f "$P/line/baresip-dev" -T -c > "$T/line.log" 2>&1 &
LINE=$!
sleep 1
VF_STATE="$T/state" VF_NOTIFY=0 VF_CALLS=1 VF_IGNORE_QUIET=1 VF_CALLS_LOG="$T/calls.jsonl" VF_CTRL=127.0.0.1:4445 VF_SPOOL="$T/spool" VF_ALWAYS_CALL=1 VF_BATCH_WAIT=2 \
  VF_PHONE=sip:phone@$ADDR:5074 \
  "$P/.venv/bin/python" "$P/foremand/foremand.py" > "$T/foremand.log" 2>&1 &
FOREMAND=$!
sleep 2

# 3. A session "finishes".
cat > "$T/spool/1-Stop.json" <<'EOF'
{"hook_event_name": "Stop", "session_id": "calltest", "cwd": "/home/me/projects/shop-api",
 "last_assistant_message": "Fixed both review comments on item 552 and all 48 tests pass. The pull request is ready for you to merge."}
EOF

sleep 30
curl -s -o /dev/null "http://127.0.0.1:8001/?%2Fhangup"  # hang up like you would; the recap follows
sleep 30
kill $FOREMAND $LINE $PHONE 2>/dev/null || true

echo "== foremand log"; cat "$T/foremand.log"
if [ -s "$T/heard.wav" ]; then
  ffmpeg -loglevel error -y -i "$T/heard.wav" -f s16le -ar 16000 -ac 1 "$T/heard.raw"
  echo "== what the phone heard (transcribed)"
  curl -sf -X POST 127.0.0.1:8765/stt --data-binary @"$T/heard.raw"; echo
else
  echo "== the phone recorded nothing; see $T/phone.log and $T/line.log"
fi
echo "== text the phone received"; tr '\r' '\n' < "$T/phone.log" | grep -iA1 "message from" | head -4
