# Venom WhatsApp bridge

Self-hosted WhatsApp for Venom — no phone-side automation. Links your WhatsApp
as a **Linked Device** (WhatsApp Web protocol, via
[Baileys](https://github.com/WhiskeySockets/Baileys)) and exposes a localhost
HTTP API that Venom's voice loop calls to send messages. Your phone keeps
working normally.

## Run

```bash
npm install
WA_PORT=8788 NTFY_TOPIC=venom-notif-xxxx node index.js
```

On first run it prints a QR code — open **WhatsApp → Settings → Linked Devices →
Link a device** and scan it. The session is saved under `state/` and survives
restarts.

## HTTP API (localhost only)

| Method | Route              | Purpose                                              |
|--------|--------------------|------------------------------------------------------|
| GET    | `/health`          | `{connected, loggedIn, user, contacts, hasQR}`       |
| GET    | `/qr`              | current login QR as ASCII (empty once linked)        |
| GET    | `/qr.png`          | current login QR as PNG                               |
| GET    | `/contacts?q=NAME` | fuzzy contact matches `[{jid, name}]`                |
| POST   | `/send`            | `{to, text}` — `to` = name \| number \| jid, or omit to reply to the last chat |

`POST /send` returns `300` with a `candidates` list when a name is ambiguous, so
the caller can ask which one.

## Environment

| Var           | Default             | Meaning                                       |
|---------------|---------------------|-----------------------------------------------|
| `WA_STATE_DIR`| `./state`           | creds + learned contact map                   |
| `WA_HOST`     | `127.0.0.1`         | HTTP bind address                             |
| `WA_PORT`     | `8788`              | HTTP port                                     |
| `WA_TOKEN`    | *(none)*            | if set, required in the `X-Token` header      |
| `NTFY_SERVER` | `https://ntfy.sh`   | ntfy base for forwarding incoming messages    |
| `NTFY_TOPIC`  | *(none)*            | ntfy topic; incoming 1:1 messages POST here so Venom chimes + reads them |

Incoming messages are forwarded to the same ntfy topic Venom's `NotificationHub`
already listens on, so this replaces the phone/MacroDroid forwarder for incoming
too. Point `NTFY_TOPIC` at your `phone.notify_topic` and disable the phone-side
forwarder to avoid duplicates.
