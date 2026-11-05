# Voice foreman

Claude on a real phone call, over [Tailscale](https://tailscale.com). It goes by **Omarchy**. It calls your phone when a Claude Code session finishes or needs you, gives a short spoken report, and takes the next task by voice. You can also call it from your phone, or talk to it at the PC.

Everything runs on your own PC: a SIP line ([baresip](https://github.com/baresip/baresip)), speech recognition and a voice on your GPU, and a brain that's `claude -p` on your own Claude login. The call goes straight from your PC to your phone across your tailnet. There's no phone number, no telephony provider and no cloud speech service.

The bar widget for [Omarchy](https://omarchy.org) is a separate repo: [omarchy-voice](https://github.com/YassineSawahi/omarchy-voice).

## What it does

- **Calls you.** A session that needs you (a permission prompt, a question) rings after 10 s. Finished sessions are grouped and ring after 90 s. Replying to the session at the PC cancels the call. No answer or declined: it tries again after 15 minutes. By default it always calls; set it to only call when you're away (the screen is locked, or the PC has been idle for 3 minutes).
- **Talks like a phone call.** It waits for your "hello?", answers in one to three short sentences, says "Let me check." before looking something up, and plays waiting music while it works. Talk over it to stop it. A cough or bump doesn't count, and a sentence that trails off ("text me a report about the…") waits a moment for the rest.
- **Reads, never writes.** The brain can look at files, disk, processes, git, your Claude Code sessions and the web, through an allowlist of read-only commands. To change anything it hands the work to a Claude Code session (`SendMessage`, or `claude --bg` in a folder), and only after a spoken yes.
- **Works after the call.** What it promised ("I'll do that after we hang up") gets done, and the result is texted to your phone. If it needs more from you, it calls you back.
- **Remembers.** A recap of each call is texted to you and kept. The day's last five recaps go into each new call.
- **Callbacks and preferences.** "Call me back in twenty minutes." "Don't call me about the billing session." "Just text me when the docs one finishes."
- **Quiet hours and Do Not Disturb.** News waits for one digest call afterwards. Callbacks you asked for still ring.
- **Keypad.** 1 repeats, 0 stops it talking, 9 hangs up. Your PIN then # unlocks stopping a session for ten minutes.
- **Phone or PC.** Talk at the PC through its mic and speakers. "Move to my PC" on a call carries the conversation to the desk; "call me" at the desk carries it back to the phone.
- **Only your phone gets in.** Any other caller is hung up before the brain starts, logged, and shown as a desktop notification. Your tailnet policy is the first lock.

## What you need

- A Linux PC with an NVIDIA GPU, Docker with the NVIDIA container toolkit, PipeWire, `baresip`, `ffmpeg`, `jq` and Python 3.
- [Claude Code](https://claude.com/claude-code), logged in. The brain runs `claude -p` on your plan (Opus 5.5 at medium effort by default; change it with `VF_MODEL` and `VF_EFFORT`).
- Tailscale on the PC and the phone.
- An Android phone with the **Baresip** app (`com.tutpro.baresip`).
- Optional: [Omarchy](https://omarchy.org) with the [omarchy-voice](https://github.com/YassineSawahi/omarchy-voice) bar widget. It reads the lock screen and Do Not Disturb from Omarchy, and with [omarchy-phone](https://github.com/YassineSawahi/omarchy-phone) also installed, it knows when you've been idle. Without Omarchy it can't tell when you're away, so keep it calling always.

## Set up

```sh
git clone https://github.com/YassineSawahi/voice-foreman.git && cd voice-foreman
python3 -m venv .venv && .venv/bin/pip install numpy websockets
sh tools/fetch-models.sh                  # the Kokoro voice, ~350 MB
docker compose build speech
```

Tell it where your phone is, in `~/.config/voice-foreman/env`:

```sh
VF_PHONE=sip:phone@100.64.0.2:5062        # your phone's Tailscale address and Baresip port
# VF_LINE_ADDRESS=100.64.0.1              # this PC; default: `tailscale ip -4`
# VF_PHONE_ADB=192.168.1.20:5555          # where the probe finds the phone over ADB; default: its Tailscale address
# VF_STT_WORDS=billing,docs               # names the speech recognition should expect
```

Then install everything as user services that start with your desktop session and restart if they crash:

```sh
sh tools/install-services.sh    # the line config, the audio devices, the services, and vf-ctl in ~/.local/bin
sh tools/install-hooks.sh       # Claude Code hooks: Stop, Notification, UserPromptSubmit (backs up settings.json)
```

Sessions that were already open pick up the new hooks only after a restart or `/hooks`.

On Omarchy, add the bar widget: `omarchy plugin add https://github.com/YassineSawahi/omarchy-voice.git --enable`. It finds `vf-ctl` in `~/.local/bin` by itself.

### Phone

- **Baresip:** Listen Address `<the phone's Tailscale IP>:5062`, Address Family IPv4, transports `udp,tcp`, Start Automatically on, Battery Optimizations off. Add an account `sip:phone@<the phone's Tailscale IP>` with Register off and Answer Mode manual.
- **Port 5062:** EMUI (Huawei and Honor phones) reserves ports 5060–5061 and 31000–31900. Listening on the Tailscale address only, not `0.0.0.0`, keeps Baresip working across Wi-Fi changes.
- **Background:** exempt Baresip and Tailscale from battery optimisation. On EMUI, also set both to "Manage manually" under App launch.
- **Calling it:** call `sip:claude@<this PC's Tailscale IP>` from Baresip.

### Tailnet policy

The phone needs SIP and the call audio on the PC; the PC needs only SIP on the phone:

```hujson
"hosts": { "my-pc": "100.64.0.1", "my-phone": "100.64.0.2" },
"grants": [
  { "src": ["my-phone"], "dst": ["my-pc"], "ip": ["udp:5060", "tcp:5060", "udp:40000-40019"] },
  { "src": ["my-pc"], "dst": ["my-phone"], "ip": ["udp:5062", "tcp:5062"] },
],
```

## Running

```sh
systemctl --user status vf-line vf-foremand vf-speech vf-probe.timer
tail -f ~/.local/state/voice-foreman/foremand.log      # calls, transcripts, decisions
tail ~/.local/state/voice-foreman/probe.log            # phone health every 3 minutes
```

Control it from the bar widget, or with `bin/vf-ctl`: `vf-ctl power on|off`, `vf-ctl calls toggle`, `vf-ctl mode always|away`, `vf-ctl call-me`, `vf-ctl desk toggle`, `vf-ctl quiet 22:00-07:00`, `vf-ctl dnd on`, `vf-ctl pin 1234`. With no arguments it prints the state as JSON.

`tools/probe.py` runs every 3 minutes. It sends the phone a silent SIP OPTIONS, which doesn't ring. If Baresip on the phone has stopped answering and ADB reaches the phone, it restarts the app. A phone locked with a PIN can't be repaired until you unlock it.

Prompts inside sessions can't be answered by voice. `tools/enable-remote-control.sh` starts Remote Control in every new Claude Code session, so you can answer them from the Claude app.

## Layout

| Path | What | Runs |
|---|---|---|
| `speech/` | Speech to text (faster-whisper large-v3-turbo), the voice (Kokoro `af_heart`), end-of-speech detection (Silero), on the GPU | Docker (`compose.yaml`), `vf-speech.service` |
| `foremand/` | The call handler: audio bridge, the brain (`claude -p` with read-only tools), calling you, the Claude Code hook (`hook.py`), the persona (`prompts/brain.md`) | `.venv`, `vf-foremand.service` |
| `line/baresip/` | The SIP line: this PC's Tailscale address, port 5060; local control on 4444 and 8000; audio through the `vf_rx`/`vf_tx` virtual devices | `vf-line.service` |
| `line/baresip-dev/` | A second line for tests, port 5070; local control on 4445 and 8001 | The tests |
| `bin/` | `vf-ctl` (state and switches), `vf-text`, `vf-callback`, `vf-session`, `vf-pref`, `vf-stop-session` (the brain's tools) | |
| `systemd/` | The user units and the PipeWire config for the two virtual audio devices | `tools/install-services.sh` |
| `tools/` | Installers, `configure-line.sh`, `fetch-models.sh`, `probe.py`, `make_hold_music.py`, `phase1.py` (a bare test call), and the tests | |
| `sounds/hold.wav` | The waiting music, generated from scratch by `tools/make_hold_music.py` | |

The line configs are written from the `.in` templates by `tools/configure-line.sh`. Run it again if an address changes, then `systemctl --user restart vf-line`.

## Tests

None of them ring your phone or show notifications, and each uses its own state folder.

- `.venv/bin/python tools/bargetest.py`: talking over it, pauses mid-sentence, noises. Offline, takes about a second.
- `tools/talktest.sh "question"`: a fake caller asks, Omarchy answers, and the answer is transcribed. `TALK_WAV=file.wav` plays a prepared script instead.
- `tools/calltest.sh`: a fake session finishes and Omarchy calls a fake phone.
- `tools/desktest.sh`: desk talks and handoff in both directions, with throwaway audio devices.

The last three need the speech service and the virtual audio devices. They run a real brain, so they use your Claude plan.

## Configuration

Everything is a `VF_*` environment variable, read from `~/.config/voice-foreman/env` by the services. The full list with defaults is `CONFIG` at the top of `foremand/foremand.py`. The persona is `foremand/prompts/brain.md`: tell it there how you like to be addressed.

## Remove

From the clone:

```sh
systemctl --user disable --now vf-line vf-foremand vf-speech vf-probe.timer
rm ~/.config/systemd/user/vf-*.service ~/.config/systemd/user/vf-probe.timer ~/.local/bin/vf-ctl
systemctl --user daemon-reload
rm ~/.config/pipewire/pipewire-pulse.conf.d/voice-foreman.conf && systemctl --user restart pipewire-pulse
docker compose down --rmi all -v                       # the speech container, its image and the Whisper cache
rm -rf ~/.config/voice-foreman ~/.local/state/voice-foreman
```

Then remove the three hooks that run `foremand/hook.py` with `/hooks` in Claude Code (or edit `~/.claude/settings.json`), and delete the clone.

## License

MIT
