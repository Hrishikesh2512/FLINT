# Getting Carnage onto the phone

Four things need a human with the phone in their hand. Everything after that
is one command.

## 1. Install three apps — from F-Droid, not the Play Store

The Play Store versions of Termux have been unmaintained since 2020 and will
fail in confusing ways. Get [F-Droid](https://f-droid.org/) first, then from
inside it:

| App | Why |
|---|---|
| **Termux** | the Linux environment Carnage runs in |
| **Termux:API** | the bridge to the battery, GPS and SMS — without it she has no phone skills |
| **Termux:Boot** | starts her again after a reboot |

> All three must come from **the same source**. Android refuses to let a
> Play-Store Termux talk to an F-Droid add-on, and the symptom is the add-on
> silently doing nothing.

## 2. Open Termux:Boot once

Just open it and close it. Until it has been launched at least once, Android
never delivers it the boot event, and Carnage will not come back after a
restart. This catches everyone.

## 3. Run the installer

In Termux:

```bash
curl -fsSL https://raw.githubusercontent.com/Hrishikesh2512/FLINT/v2/rebuild/carnage/provisioning/install.sh | bash
```

It installs Python, clones the repo, installs both packages, writes a config
with a freshly generated sync token, registers the boot hook, and then proves
it worked by running `python -m carnage --once`.

Safe to run again — that is also how you update.

## 4. Grant the permissions she needs

Termux:API asks for these the first time each is used, and a denied permission
is not an error — the matching skill just stays off and she says so honestly.

| Permission | What stops working without it |
|---|---|
| Location | `where_am_i`, and the location line in an SOS |
| SMS | `send_text` and `sos_sms` |
| Notifications | `check_notifications` |

The fastest way to trigger all three is to run `python -m carnage` and ask her
for each one once.

Battery optimisation is the other thing worth doing by hand: Android will
otherwise kill a backgrounded Termux within the hour. **Settings → Apps →
Termux → Battery → Unrestricted.**

---

## Connecting the Pi

The installer prints the exact `[sync]` block to paste into
`/etc/venom/venom.toml`, with the token already filled in. Then:

```bash
sudo systemctl restart venom
```

**Use the Tailscale address if you have it.** The Pi moves between your home
Wi-Fi and the phone's own hotspot, so its route to the phone changes under it;
a Tailscale name does not.

Worth knowing: when the Pi is on the phone's hotspot, the phone *is* the
gateway, so it is reachable at the hotspot address even with no internet at
all — the two of them stay in step on a walk with no signal.

## Checking it works

On the phone:

```bash
python -m carnage --once     # what she found, as JSON
tail -f ~/.carnage/carnage.log
```

On the Pi:

```bash
journalctl -u venom -f | grep sync
```

A successful exchange logs like `sync: carnage — sent 3, received 1.` Silence
for more than the sync interval means she cannot reach the phone; a refusal
logs loudly and always means the token or the peer list, never the network.

## What lives where

```
~/FLINT/                 the checkout, updated by re-running the installer
~/.carnage/carnage.json  config — the token lives here, mode 600
~/.carnage/memory.json   the facts she keeps in every prompt
~/.carnage/archive.db    everything else she has ever been told
~/.carnage/carnage.log   what she has been doing
~/.termux/boot/carnage   the boot hook
```

## If something is wrong

**"no phone platform detected"** — Termux:API is missing, or it came from a
different source than Termux. Check with `termux-battery-status`; if that
command does not exist, the add-on is not really installed.

**She stops when the screen locks** — the boot script takes a wake lock, but a
manually started `python -m carnage` does not. Run `termux-wake-lock` first,
and set the battery mode to Unrestricted.

**The Pi cannot reach her** — the phone's IP moved. That is what Tailscale is
for; otherwise re-read the address with `ifconfig` and update `venom.toml`.

**A sync is refused** — the token in `venom.toml` does not match the one in
`carnage.json`, or `venom` is missing from the `peers` list. The Pi's log says
which.
