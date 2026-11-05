#!/bin/sh
# Add the voice foreman's hooks to ~/.claude/settings.json: Stop and Notification (a session finished
# or needs you) and UserPromptSubmit (you replied, so don't call about that session).
# Backs the file up first, only touches the "hooks" key, and skips hooks that are already there.
# Undo: restore ~/.claude/settings.json.bak-voice-foreman, or remove the entries in /hooks.
set -e
F=$HOME/.claude/settings.json
P=$(cd "$(dirname "$0")/.." && pwd)
CMD="python3 -I $P/foremand/hook.py"
EVENTS="Stop Notification UserPromptSubmit"

missing=""
for ev in $EVENTS; do
  jq -e --arg e "$ev" --arg c "$CMD" '[.hooks[$e][]?.hooks[]?.command] | index($c)' "$F" >/dev/null || missing="$missing $ev"
done
[ -z "$missing" ] && { echo "already installed"; exit 0; }

cp "$F" "$F.bak-voice-foreman"
cp "$F" "$F.tmp"
for ev in $missing; do
  jq --arg e "$ev" --arg c "$CMD" '
    .hooks[$e] = ((.hooks[$e] // []) + [{hooks: [{type: "command", command: $c, async: true, timeout: 10}]}])' \
    "$F.tmp" > "$F.tmp2" && mv "$F.tmp2" "$F.tmp"
done
for ev in $EVENTS; do
  jq -e --arg e "$ev" --arg c "$CMD" '[.hooks[$e][].hooks[].command] | map(select(. == $c)) | length == 1' "$F.tmp" >/dev/null
done
mv "$F.tmp" "$F"
echo "added:$missing; backup at $F.bak-voice-foreman"
