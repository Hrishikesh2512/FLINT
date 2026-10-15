# Shield

One place for everything your AIs know and make, and the phone's file
explorer. An Android app; reachable from anywhere over Tailscale.

## What it does

- **Files**: Shield's own tree of folders, notes, files and links. Search
  covers names, the words inside notes and text files, and tags.
- **Phone**: the whole phone's storage (Downloads, Camera, Screenshots,
  Documents, WhatsApp media). Open, share, rename, delete, or **Save to
  Shield** so your AIs can use a file. Needs "All files access".
- **Starred, From your AIs, Recent, Activity, Trash**: views across
  everything. Every item shows who made it; Activity shows who did what.
- **Share to Shield** from any app lands in Inbox.
- **Fills itself**: new screenshots, downloads, documents and WhatsApp
  documents come into Inbox on their own (`AutoFill.kt`; Settings to switch
  off). Jarvis then names each one, files it, tags it, writes what it is, and
  turns dates in it (expiries, due dates) into reminders.
- **Jarvis's memory**: under /Jarvis, what she knows about you, her lists,
  notes, reminders and imported WhatsApp chats. Edit those notes and she
  learns the change.
- **In a browser**: open `http://<phone>:8791` anywhere that can reach the
  phone, and paste your key from Settings. The key acts as you; the AI token
  never does.

## For AIs

An HTTP API on port 8791 (`Api.kt`), token required. Every request names its
AI in `X-Shield-Agent`, which is recorded against everything it touches. AIs
can trash things but never delete for good; only you empty the Trash.

Python: [`packages/shield-client`](../packages/shield-client). Every body of
Jarvis has the same tools (`flint_core.skills.shield`): search, read, list,
save, organise. On the phone she gets the token by herself (a permission only
apps signed with the same key can hold). On the Pi, add to venom.toml:

```toml
[shield]
url   = "http://<phone>:8791"
token = "<token from Shield → Settings>"
```

On the PC, put `shield_url` and `shield_token` in `config/api_keys.json`.

The address, token and a ready-to-paste Python line are in Shield → Settings.

## Building

```bash
cd shield/android && gradle assembleDebug
```

Sign it with the same key as Carnage, or Jarvis cannot get the token.
