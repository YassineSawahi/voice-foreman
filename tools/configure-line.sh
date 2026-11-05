#!/bin/sh
# Write the baresip line configs (line/baresip and line/baresip-dev) from their .in templates,
# with this PC's Tailscale address and your phone's SIP address. Both come from
# ~/.config/voice-foreman/env:
#   VF_PHONE=sip:phone@100.64.0.2:5062   your phone (required)
#   VF_LINE_ADDRESS=100.64.0.1           this PC (default: `tailscale ip -4`)
# Run it again whenever either address changes, then: systemctl --user restart vf-line
set -e
P=$(cd "$(dirname "$0")/.." && pwd)
ENV=$HOME/.config/voice-foreman/env
[ -f "$ENV" ] && . "$ENV"
ADDR=${VF_LINE_ADDRESS:-$(tailscale ip -4 2>/dev/null | head -1)}
[ -n "$ADDR" ] || { echo "no Tailscale address for this PC: is Tailscale up? Or set VF_LINE_ADDRESS in $ENV" >&2; exit 1; }
[ -n "${VF_PHONE:-}" ] || { echo "set VF_PHONE (your phone's SIP address, e.g. sip:phone@100.64.0.2:5062) in $ENV" >&2; exit 1; }
for line in baresip baresip-dev; do
  for f in config accounts contacts; do
    sed -e "s|{{ADDRESS}}|$ADDR|g" -e "s|{{PHONE}}|$VF_PHONE|g" "$P/line/$line/$f.in" > "$P/line/$line/$f"
  done
done
echo "line configured: this PC $ADDR, your phone $VF_PHONE"
