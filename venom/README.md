# Venom — the Raspberry Pi wearable runtime of FLINT

Venom turns a **completely blank Raspberry Pi 4** into a dedicated FLINT wearable
appliance that boots entirely from a USB pendrive. No SD card, no keyboard, no
monitor, no pre-installed OS — you flash one pendrive on your laptop, plug it
in, and power on.

The Pi never runs large AI models. It is the *orchestrator*: it watches its own
health (network, USB headset, battery-friendly services) and resolves where its
brain lives right now — **your laptop when reachable, cloud APIs otherwise**.

```
venom/
├── src/venom/            # the appliance daemon (stdlib-only, ~10 MB RSS)
│   ├── supervisor.py     #   asyncio loop: probe → resolve brain → publish status
│   ├── monitors/         #   network probe, USB-headset detection, brain resolver
│   ├── status.py         #   atomic /run/venom/status.json snapshots
│   └── sdnotify.py       #   systemd readiness + watchdog heartbeat
├── tests/                # unit tests (run anywhere: Windows, CI, the Pi)
└── provisioning/         # blank-pendrive → running-appliance kit
    ├── prepare-pendrive.ps1      # run on Windows after flashing
    ├── install-firstboot.sh      # stage 1: runs on the Pi's first boot
    ├── provision.sh              # stage 2: installs everything over Wi-Fi
    ├── venom.service             # the daemon unit (watchdog, memory caps)
    ├── venom-provision.service   # one-shot installer unit
    └── venom.toml                # appliance config (brain priority list)
```

## Requirements

- Raspberry Pi 4 (2 GB is enough — the daemon is capped at 300 MB and idles far below)
- A USB pendrive (16 GB+; your 32 GB stick is ideal) — this becomes the entire disk
- USB headset with microphone
- Wi-Fi credentials, and (recommended) your laptop's LAN or Tailscale IP
- **One-time check:** Pi 4 boards ship with USB boot enabled in bootloader
  EEPROMs from **2020-09 onward**. If your Pi was bought after that, skip this.
  If it refuses to boot from USB, you need a one-time EEPROM update using any
  SD card (Raspberry Pi Imager → *Misc utility images → Bootloader → USB Boot*).

## From blank pendrive to talking appliance

**1. Flash the pendrive** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):

- OS: *Raspberry Pi OS Lite (64-bit)* (under "Raspberry Pi OS (other)")
- Storage: your pendrive
- When asked "Would you like to apply OS customisation settings?" → **Edit settings**:
  - Hostname: `venom`
  - Username/password: pick yours (this is your SSH login)
  - **Configure wireless LAN**: your Wi-Fi SSID + password, country `IN`
  - Services tab: **Enable SSH** (password authentication)
- Write. Keep the pendrive plugged in afterwards (re-insert if Windows ejected it).

**2. Add Venom** — from this repo on your laptop:

```powershell
cd venom\provisioning
.\prepare-pendrive.ps1 -LaptopHost <your laptop's LAN/Tailscale IP>
```

The script finds the pendrive's boot partition automatically, copies the
provisioning payload, and chains Venom onto the Imager first-boot script.

**3. Boot the Pi** — pendrive in a **USB 3 (blue) port**, headset in the other,
power on. Leave it alone for ~10 minutes on first boot:

- *boot 1*: filesystem expands; Imager applies hostname/user/Wi-Fi/SSH; the
  Venom hook installs the provisioning service
- *boot 2*: `venom-provision` waits for Wi-Fi, installs system packages, clones
  this repo (branch `v2/rebuild`), installs the daemon, and starts it. If Wi-Fi
  isn't reachable it simply retries on every boot until it succeeds.

**4. Verify from your laptop:**

```bash
ssh <username>@venom.local
systemctl status venom               # active (running), Type=notify, watchdog armed
cat /run/venom/status.json           # {"internet": true, "headset": "...", "brain": "laptop", ...}
journalctl -u venom -f               # live transitions (brain switches, headset events)
```

If anything went wrong during install: `journalctl -u venom-provision`.

## How the brain resolver works

`/etc/venom/venom.toml` lists brain candidates with priorities (lower wins):
laptop first (priority 0), then Gemini/Groq/OpenAI/Anthropic/OpenRouter
endpoints. Every cycle the daemon:

1. keeps the current brain if it's still healthy (no flapping mid-conversation),
2. but lets a **higher-priority** candidate take over — so when your laptop
   comes back in range, Venom switches back to it automatically,
3. falls to the next reachable candidate when the current one dies,
4. reports `brain: null` (offline mode) when nothing answers.

Edit the file on the Pi and `sudo systemctl restart venom` to apply.

## Reliability & battery design

- `Type=notify` + `WatchdogSec=90`: if the daemon ever hangs, systemd restarts it.
- `Restart=always`: crashes self-heal; provisioning is idempotent and re-runs
  until it succeeds.
- Journald runs in volatile (RAM) mode, capped at 32 MB — no log wear on the
  pendrive, no unbounded growth.
- Status lives in `/run` (tmpfs): zero flash writes per cycle.
- Display blanking is enabled; the daemon is stdlib-only and idles at a few MB.

## Development (any OS)

```bash
pip install -e ./venom
python -m pytest venom/tests     # 23 tests, no hardware needed
python -m venom --once           # one real health cycle, prints JSON
python -m venom -v               # run the daemon in the foreground
```

## What lands here next

This daemon is the appliance skeleton (Phase 3 of the FLINT v2 plan). The next
increments plug into the supervisor: wake word (openWakeWord) → voice activity
detection (Silero VAD) → audio streaming over the Flint Link to the resolved
brain — laptop GPU first, cloud otherwise, never on the Pi itself.
