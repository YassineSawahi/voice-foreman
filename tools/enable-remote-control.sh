#!/bin/sh
# Start Remote Control in every new Claude Code session, so you can answer a session's permission or
# question prompts from the Claude app on your phone. Omarchy tells you about them; the app answers them.
# Backs up ~/.claude/settings.json first. Undo: set "remoteControlAtStartup" to false (or restore the backup).
set -e
F=$HOME/.claude/settings.json
umask 077  # settings.json can hold tokens: the copies stay yours alone
cp -p "$F" "$F.bak-remote-control"
jq '.remoteControlAtStartup = true' "$F" > "$F.tmp" && chmod --reference="$F" "$F.tmp" && jq -e '.remoteControlAtStartup == true' "$F.tmp" >/dev/null && mv "$F.tmp" "$F"
echo "Remote Control will start in new sessions. Open the Claude app on the phone (Code tab) to see them."
