# Getting her on the phone without installing anything

Run Carnage on a machine you already have. Open a link on the phone. Add it to
the home screen. It launches from an icon, full screen, with no browser bars —
and there was no app store, no APK and no terminal anywhere in that.

The browser gives her three of the four things that made a phone body worth
having: **GPS**, **battery**, and **your voice**. The fourth — sending an SMS
without you — a web page genuinely cannot do, and the section at the bottom is
honest about what that costs.

## Why it needs Tailscale

Browsers refuse geolocation, the microphone, service workers and
installability on any plain-`http://` origin that is not `localhost`. That is a
hard rule with no flag to turn it off, and those four *are* the feature — so
`http://192.168.1.x:8791` gives you a page that can do none of them.

You need a real certificate. `tailscale serve` gives you one on a name only
your tailnet can reach: no port forwarding, nothing exposed to the internet,
no self-signed warning to click through. You already have Tailscale.

## Setup

**1. Turn on two tailnet features** — once each, in the Tailscale admin:

- **HTTPS certificates** at
  [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns) →
  *Enable HTTPS*
- **Serve**, which is off by default. Running `tailscale serve` the first time
  prints the exact enable link for your tailnet; follow it and run the command
  again.

Both are account settings rather than anything on this machine, so they cannot
be scripted — and both are one click.

**2. Start her** on whichever machine will hold her:

```bash
pip install -e packages/flint-core -e "carnage[hub]"
python -m carnage --web
```

**3. Publish the page** over the tailnet:

```bash
tailscale serve --bg 8791
```

It prints the URL — `https://<machine>.<tailnet>.ts.net`.

**4. Open it on the phone**, with the token from `~/.carnage/carnage.json`:

```
https://<machine>.<tailnet>.ts.net/?k=<the hub token>
```

The page stores the key and strips it from the address immediately, so it does
not sit in history or travel if you share the link.

**5. Add to Home Screen** — Chrome's ⋮ menu, or Share → Add to Home Screen on
iOS. From then on it opens from the icon like any other app.

## What she can do from a page

| | |
|---|---|
| Everything in her shared memory | ✅ the same facts as your other devices |
| Conversation, with tools | ✅ text, or dictated with the mic button |
| Where you actually are | ✅ real GPS, not a city guess |
| Battery | ✅ |
| Her voice back | ✅ the phone's own speech synthesis |
| Her other bodies | ✅ sees them, hands work to them |
| **Send an SMS** | ⚠️ **opens your messaging app pre-filled — you tap send** |
| Read the notification shade | ❌ no web API exists |
| Run with the screen off | ❌ a page is not a background service |

## The SMS caveat, stated properly

This is the one place the browser route is genuinely worse, and it is worth
being blunt because it touches SOS.

A web page cannot put a message on the cellular network. It can open your
messaging app with the recipient and body filled in; you press send. So when
Carnage is browser-bodied she **never says "sent"** — she says *"tap send"*,
and an SOS leads with `Tap send — they are NOT sent until you do`, with a
button per contact.

That is a real degradation of the emergency path. Sending still works with no
internet, because SMS rides the cellular network either way — but it needs your
thumb, and if you are unconscious it does not happen. If that matters to you,
use [the Termux route](README.md) for real sending; the two can coexist, and
everything else in this document stays true.

## Keeping it up

`tailscale serve --bg` survives reboots on its own. For Carnage itself:

```bash
# Linux, if she is on a machine with systemd
systemctl --user enable --now carnage    # after writing a unit
```

On Windows, Task Scheduler with "run at logon" is the least effort.

If she is on a laptop that sleeps, she is asleep too — the page will say
"offline" and the Pi's syncs will queue rather than fail. That is the real cost
of not running her on the phone itself, and it is the argument for Termux if
you want her always awake.

## Troubleshooting

**"Add to Home Screen" is missing** — you are on `http://`, or the manifest did
not load. Check the URL starts `https://` and that `/manifest.webmanifest`
returns JSON.

**The mic button says the browser has no dictation** — Firefox Android has no
Web Speech API. Chrome does.

**The dot stays red** — the page cannot reach the API. Usually the token: open
the `?k=` link again.

**She says she has no key to think with** — put a Gemini API key in
`~/.carnage/carnage.json`. Everything else works without one; conversation does
not.

**Location says it cannot see you** — the permission prompt was dismissed. Site
settings → Location → Allow, then reload.
