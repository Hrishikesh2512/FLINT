# Getting her on the phone without installing anything

Run Carnage on a machine you already have. Open a link on the phone. Add it to
the home screen. It launches from an icon, full screen, with no browser bars —
and there was no app store, no APK and no terminal anywhere in that.

> ## Read this before you choose this route
>
> **She is only awake while that machine is.** Serving the page from a laptop
> means she sleeps when the lid closes, and the phone shows an icon that opens
> onto nothing. That is not a bug to be fixed here; it is what "run her
> somewhere else" means.
>
> If you want an assistant that is simply *there* — the entire point of a
> phone — she has to run on the phone, which is the
> **[Termux route](README.md)**. It costs three F-Droid apps and one command,
> and it is better on every axis afterwards: no laptop, no certificate, no
> `chrome://flags`, no firewall rule, and real SMS instead of "tap send". The
> page is served to `http://localhost`, which browsers treat as a secure
> context by definition, so everything below stops being necessary.
>
> This route is the right one for exactly two cases: trying her out in the next
> five minutes, or reaching her from a device that cannot run Termux — a
> desktop, a tablet, an iPhone.

The browser gives her three of the four things that made a phone body worth
having: **GPS**, **battery**, and **your voice**. The fourth — sending an SMS
without you — a web page genuinely cannot do, and the section at the bottom is
honest about what that costs.

## The one constraint

Browsers refuse geolocation, the microphone, service workers and
installability on any plain-`http://` origin that is not `localhost` — and
those four *are* the feature. So the page has to reach the phone as a **secure
context**, and there are two ways to get there.

**Route A — one flag on the phone.** No account changes, nothing to enable,
works today. Chrome will treat a named origin as secure if you tell it to.

**Route B — a real certificate.** Nicer, permanent, and the only option on
iOS, but it needs two clicks in the Tailscale admin.

Route A is below; Route B is after it. Both serve the same page.

---

# Route A — one flag, no account changes

**1. Bind the page to something the phone can reach.** In
`~/.carnage/carnage.json`:

```json
"web": { "enabled": true, "host": "0.0.0.0", "port": 8791, "token": "<the hub token>" }
```

Then `python -m carnage`.

**2. Use the Tailscale IP, not the LAN one.** `tailscale ip -4` gives it —
`100.125.31.79` on this machine. It is stable, unlike a LAN address that
changes every time you join a different network, and it works from the phone
anywhere the tailnet reaches.

**3. Tell Chrome on the phone to trust that origin.** Open `chrome://flags`,
search *Insecure origins treated as secure*, put the origin in the box, set the
dropdown to **Enabled**, and relaunch:

```
http://100.125.31.79:8791
```

That grants the origin secure-context privileges — geolocation, microphone,
service worker, and the install prompt — for that browser only. Nothing is
exposed to the internet; the address is only routable inside your tailnet.

**4. Open it** with the token, then **Add to Home Screen**:

```
http://100.125.31.79:8791/?k=<the hub token>
```

> **Chrome only, Android only.** Safari and iOS have no equivalent flag, so an
> iPhone needs Route B. The flag is a developer setting: it persists, but a
> major Chrome update can clear it, and you would then re-add it.

---

# Route B — a real certificate over Tailscale

Better if you want it permanent, and required on iOS.

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

**2. Start her** on whichever machine will hold her (loopback is fine here —
Tailscale proxies to it):

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

**"Add to Home Screen" is missing** — the origin is not a secure context. On
Route A, check the flag origin matches the address bar *exactly*, port
included, and that you relaunched Chrome. On Route B, check the URL really is
`https://` and that `/manifest.webmanifest` returns JSON.

**Location or the mic is refused on Route A** — same cause. Chrome only treats
the exact origin string you typed into the flag as secure; `http://100.125.31.79:8791`
and `http://exodus:8791` are different origins.

**The mic button says the browser has no dictation** — Firefox Android has no
Web Speech API. Chrome does.

**The dot stays red** — the page cannot reach the API. Usually the token: open
the `?k=` link again.

**She says she has no key to think with** — put a Gemini API key in
`~/.carnage/carnage.json`. Everything else works without one; conversation does
not.

**Location says it cannot see you** — the permission prompt was dismissed. Site
settings → Location → Allow, then reload.
