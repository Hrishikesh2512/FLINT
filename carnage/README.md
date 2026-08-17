# Carnage — the phone body, and the hub the others sync to

Same mind as Venom and FLINT. Different body.

Carnage runs the assistant on an Android phone. It keeps the *same* memory,
archive, projects and outcome log as the other two devices — not a copy, the
same facts, kept in step by `flint_core.sync` — and adds the three things only
a phone has: a real GPS position, a battery, and a cellular radio.

```
venom (Pi)  ─┐
             ├─▶  carnage (phone, hub)
flint (PC)  ─┘
```

## Why the phone is the hub

Not because it is the most powerful device — it isn't. Because it is the only
one that is reachable. The Pi hops onto a hotspot and changes subnet, the
laptop is asleep most of the day, and a full mesh needs every pair to be up at
the same moment. The phone is always powered, always networked, always
carried. The others are leaves; they talk to it and never to each other.

## One assistant, not two

She is the **same person** on both. The persona lives in
`flint_core.persona` and every body renders the same text — 9,000-odd
characters, byte for byte identical — with exactly one sentence substituted for
where she happens to be. `Venom` and `Carnage` are hostnames; Jarvis is who she
is, and she never refers to her other devices as separate assistants.

Each body also carries a **roster** of the others, rendered into its prompt:

```
[YOUR OTHER BODIES — you are one person in several places]
  - the wearable on his body (venom): listen on the walk; the earphone. Reachable now.
```

with the instruction that matters: *never say you can't do something one of
your other bodies can.* Presence is a fact, not an assumption — a device is
"reachable now" only because it actually synced recently; otherwise she says
when it was last seen.

And she can act on it. `ask_other_device` hands work to the body that can do
it, riding the sync connection rather than opening a second one:

> **On the Pi, no cellular radio:** "text Ma I'm running late"
> → queued for the phone → the phone sends it → the answer comes back to the Pi.

A device that is asleep queues rather than fails, and she says so specifically
instead of promising something that may not happen for hours.

## What it adds that Venom cannot have

| Skill | Why it needs a phone |
|---|---|
| `where_am_i` | GPS gives a street. The Pi's network lookup gives a city, which is never enough to know whether he has left the house. |
| `send_text` | SMS rides the cellular network. It arrives when WhatsApp cannot. |
| `sos_sms` | **The emergency path that does not need the internet.** Venom's SOS is WhatsApp-only, so it is unavailable in exactly the conditions it exists for. |
| `phone_battery` | Whether the device will still be alive in four hours. |
| `check_notifications` | Reads the shade natively — no ntfy round-trip. |

Everything else is the *same code* Venom runs, from `flint_core.skills`:
time, timers, search, weather, memory, recall, reminders, notes, lists,
connections, calendar, mail, projects, learning, background jobs, watches,
documents, git and deployment. Carnage does not reimplement any of it and does
not copy it — 31 tools on a phone, of which 4 are phone-specific.

## Running it

```bash
pip install -e packages/flint-core -e "carnage[hub]"
python -m carnage --once      # one status snapshot, then exit
python -m carnage             # run the hub until stopped
```

`--once` prints what it found — which platform, which capabilities came up —
because on a phone there is no console to read a traceback from.

On a laptop it reports `"platform": "absent"` and switches every phone skill
off. That is deliberate: the whole package is importable and testable without
an Android device in the room.

## Getting it onto a phone

Two routes, and they can coexist.

**A page, with nothing installed** — run Carnage on a machine you already have,
publish it over Tailscale for a real HTTPS certificate, and add the page to the
phone's home screen. You get her whole shared memory, conversation, dictation,
real GPS and battery. What you give up is silent SMS: a browser can only open
your messaging app pre-filled, so she says *"tap send"* and never *"sent"*.
See **[NO-DOWNLOAD.md](provisioning/NO-DOWNLOAD.md)**.

**Termux, for a phone that acts without you** — real SMS, the notification
shade, and a process that keeps running with the screen off. Three apps from
F-Droid and one command; `TermuxPhone` shells out to `termux-battery-status`,
`termux-location` and `termux-sms-send`. See
**[provisioning/README.md](provisioning/README.md)**.

**Chaquopy, properly.** A Kotlin app embeds CPython and holds a foreground
service, so the loop survives the screen locking. It passes one callable into
`platform.detect(bridge=...)` and `AndroidPhone` routes everything through it.
One bridge function rather than a binding per capability, deliberately: the
alternative puts every new skill behind an app release.

> Chaquopy is Android-only. Choosing it closes the iOS door — worth deciding
> on purpose rather than discovering later.

## Configuration

`carnage.json`, next to the state directory (`~/.carnage` by default, or
`$CARNAGE_STATE`):

```json
{
  "device": "carnage",
  "user_name": "Hrishikesh",
  "hub": { "port": 8790, "token": "a-shared-secret", "peers": ["venom", "flint"] },
  "devices": [
    { "name": "venom", "body": "the wearable on his body",
      "can": ["listen on the walk", "the earphone"] },
    { "name": "flint", "body": "on his desktop",
      "can": ["the screen", "his files and repos"] }
  ],
  "repos": [["flint", "/data/flint"]]
}
```

`devices` is what she is told about her other bodies. `can` is read out in the
prompt, so it is written the way she would say it — "send a text", not
`sms_send`.

`device` is the one field worth setting by hand. It is the name every change
is attributed to, so two installs sharing an id would each treat the other's
edits as their own and quietly eat each other's sync positions.

`peers` is an allowlist on top of the token. The token proves someone knows
the secret; the allowlist says which device ids may use it.

## Connecting a leaf

Venom has it built in — add a `[sync]` block to `/etc/venom/venom.toml`:

```toml
[sync]
enabled = true
device  = "venom"
hub     = "ws://carnage.local:8790"
token   = "a-shared-secret"

[[device]]
name = "carnage"
body = "on his phone"
can  = ["send a text", "GPS position", "emergency SMS"]
```

For anything else, one function is the whole leaf-side surface:

```python
from flint_core.syncws import sync_with_hub

sync_with_hub(engine, "ws://carnage.local:8790", token="a-shared-secret")
```

Delivery is at-least-once on purpose. A dropped connection costs a repeated
batch, never a missing one — the watermark only advances when the far side
says what it applied.

## Tests

```bash
python -m pytest carnage/tests      # 119 tests, no phone needed
```
