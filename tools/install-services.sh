#!/bin/sh
# Make the voice foreman permanent: user services that start with your desktop session and
# restart if they crash. Safe to run again after an update.
# Undo: systemctl --user disable --now vf-line vf-foremand vf-speech vf-probe.timer,
#       remove ~/.config/pipewire/pipewire-pulse.conf.d/voice-foreman.conf and ~/.local/bin/vf-ctl
set -e
P=$(cd "$(dirname "$0")/.." && pwd)
U=$HOME/.config/systemd/user
PW=$HOME/.config/pipewire/pipewire-pulse.conf.d
mkdir -p "$U" "$PW"
# State, logs and settings hold transcripts, recaps and the keypad PIN: yours alone.
mkdir -p -m 700 "$HOME/.local/state/voice-foreman" "$HOME/.config/voice-foreman"
chmod 700 "$HOME/.local/state/voice-foreman" "$HOME/.config/voice-foreman"

echo "1/5 writing the line config"
sh "$P/tools/configure-line.sh"

echo "2/5 stopping anything already running"
systemctl --user stop vf-line.service vf-foremand.service vf-probe.timer 2>/dev/null || true

echo "3/5 virtual audio devices from config (audio blips for a second; your default speakers and mic stay)"
sink=$(pactl get-default-sink)
src=$(pactl get-default-source)
cp "$P/systemd/pipewire-pulse-voice-foreman.conf" "$PW/voice-foreman.conf"
systemctl --user restart pipewire-pulse.service
sleep 2
pactl set-default-sink "$sink"
pactl set-default-source "$src"
pactl list short sinks | grep -q 'vf_rx' && pactl list short sinks | grep -q 'vf_tx' \
  || { echo "the virtual audio devices didn't come up; see journalctl --user -u pipewire-pulse"; exit 1; }

echo "4/5 installing and starting the services"
claude_bin=$(command -v claude) || { echo "claude isn't on your PATH"; exit 1; }
path="$(dirname "$claude_bin"):/usr/share/omarchy/bin:/usr/local/bin:/usr/bin"
for unit in "$P/systemd/"vf-*.service "$P/systemd/vf-probe.timer"; do
  sed -e "s|{{PROJECT}}|$P|g" -e "s|{{PATH}}|$path|g" "$unit" > "$U/$(basename "$unit")"
done
systemctl --user daemon-reload
systemctl --user enable --now vf-speech.service vf-line.service vf-foremand.service vf-probe.timer
# vf-ctl on PATH, where the omarchy-voice bar widget looks for it
mkdir -p "$HOME/.local/bin"
ln -sf "$P/bin/vf-ctl" "$HOME/.local/bin/vf-ctl"

echo "5/5 checking"
sleep 4
systemctl --user --no-pager --no-legend list-units 'vf-*'
"$P/.venv/bin/python" -I "$P/tools/probe.py" || true
tail -1 "$HOME/.local/state/voice-foreman/foremand.log" 2>/dev/null || true
