#!/bin/sh
# Write the baresip line configs (line/baresip and line/baresip-dev) from their .in templates,
# with this PC's Tailscale address, your phone's SIP address and the line's secret address.
# The two addresses come from ~/.config/voice-foreman/env:
#   VF_PHONE=sip:phone@100.64.0.2:5062   your phone (required)
#   VF_LINE_ADDRESS=100.64.0.1           this PC (default: `tailscale ip -4`)
# The line only takes calls to sip:vf<secret>@this-PC: no dash, which Baresip on Android drops from
# a contact's address. The secret is made the first time and
# kept in ~/.config/voice-foreman/line-secret; this prints the address to call from Baresip.
# To change it, delete that file, run this again and update the address on the phone.
# Run it again whenever an address changes, then: systemctl --user restart vf-line
set -e
umask 077  # the configs hold the line's secret address
P=$(cd "$(dirname "$0")/.." && pwd)
CONF=$HOME/.config/voice-foreman
ENV=$CONF/env
SECRET=$CONF/line-secret
[ -f "$ENV" ] && . "$ENV"
ADDR=${VF_LINE_ADDRESS:-$(tailscale ip -4 2>/dev/null | head -1)}
[ -n "$ADDR" ] || { echo "no Tailscale address for this PC: is Tailscale up? Or set VF_LINE_ADDRESS in $ENV" >&2; exit 1; }
[ -n "${VF_PHONE:-}" ] || { echo "set VF_PHONE (your phone's SIP address, e.g. sip:phone@100.64.0.2:5062) in $ENV" >&2; exit 1; }
mkdir -p "$CONF"
chmod 700 "$CONF"
[ -s "$SECRET" ] || od -An -N16 -tx1 /dev/urandom | tr -d ' \n' > "$SECRET"
chmod 600 "$SECRET"
LINE_USER=vf$(cat "$SECRET")
printf '%s\n' "$LINE_USER" | grep -qxE 'vf[0-9a-f]{32}' || { echo "$SECRET should hold 32 hex digits: delete it and run this again" >&2; exit 1; }
render() {  # render TEMPLATE: fill in {{ADDRESS}}, {{PHONE}} and {{LINE_USER}}
  # The values go in through the environment, which only this account can read: any account on
  # the PC can read a running program's command line, so the secret must never be on one.
  ADDRESS=$ADDR PHONE=$VF_PHONE LINE_USER=$LINE_USER awk '
    function fill(s, key, value,   out, i) {
      out = ""
      while ((i = index(s, key)) > 0) { out = out substr(s, 1, i - 1) value; s = substr(s, i + length(key)) }
      return out s
    }
    { print fill(fill(fill($0, "{{ADDRESS}}", ENVIRON["ADDRESS"]), "{{PHONE}}", ENVIRON["PHONE"]), "{{LINE_USER}}", ENVIRON["LINE_USER"]) }
  ' "$1"
}
for line in baresip baresip-dev; do
  for f in config accounts contacts; do
    out=$P/line/$line/$f
    rm -f "$out.new"
    render "$P/line/$line/$f.in" > "$out.new"
    mv -f "$out.new" "$out"  # a new file, so one an older version left readable is replaced
  done
done
echo "line configured: this PC $ADDR, your phone $VF_PHONE"
echo "In Baresip on the phone, call (and save as a contact): sip:$LINE_USER@$ADDR"
