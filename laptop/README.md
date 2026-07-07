# Venom screen-text server (laptop side)

Lets you say **"Jarvis, look at my screen"** and have her read / debug whatever
is on your laptop — an error, a stack trace, code, a log, a message.

## Why OCR and not a screenshot

The Pi's voice model (`gemini-2.5-flash-native-audio`) is **blind to images** —
verified on-device: fed a frame, it replies *"I haven't received the image."*
But it reads text perfectly. So this server does **local OCR** of your active
window and sends back only the extracted text. That's:

- **Fast** — warm model + active-window crop, ~0.1–1s per look.
- **Free** — no vision API, no quota (the thing that 429'd us).
- **Precise** — she gets the exact error string / line number, not a paraphrase.
- **Private** — only text leaves the machine, only when the Pi asks with the token.

Trade-off: text only. No colours, layout, icons, or diagrams. Great for
code/terminals/errors; not for "which button is highlighted."

## Run it

```bash
cd laptop
pip install -r requirements.txt
python screen_server.py --token YOUR_SECRET
```

It captures the **active window**, so focus what you want her to read.

## Point the Pi at it

In `/etc/venom/venom.toml` on the Pi:

```toml
[screen]
enabled = true
host    = "192.168.1.50"   # this laptop's LAN or Tailscale address
port    = 8766
token   = "YOUR_SECRET"    # must match --token above
```

Then `sudo systemctl restart venom`. Ask her to *"look at my screen"*.

## Notes

- Windows HiDPI is handled (per-monitor DPI aware).
- Very wide (4K/ultrawide) captures are downscaled to keep OCR quick; tune with
  `--max-width`.
- Autostart on the laptop: run it from Task Scheduler (Windows) or a login item.
