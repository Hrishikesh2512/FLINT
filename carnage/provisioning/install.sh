#!/data/data/com.termux/files/usr/bin/bash
#
# Put Carnage on this phone. Run it inside Termux:
#
#   curl -fsSL https://raw.githubusercontent.com/Hrishikesh2512/FLINT/v2/rebuild/carnage/provisioning/install.sh | bash
#
# Safe to run again: it pulls the latest code, keeps your existing config and
# your existing token, and re-registers the boot hook. Re-running after a
# change to the repo is the intended way to update.
set -euo pipefail

REPO_URL="${CARNAGE_REPO:-https://github.com/Hrishikesh2512/FLINT.git}"
BRANCH="${CARNAGE_BRANCH:-v2/rebuild}"
CHECKOUT="${CARNAGE_HOME:-$HOME/FLINT}"
STATE="${CARNAGE_STATE:-$HOME/.carnage}"
CONFIG="$STATE/carnage.json"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m x\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "/data/data/com.termux" ] || die "This is meant to run inside Termux."

# ── 1. packages ─────────────────────────────────────────────────────────────
say "Installing packages (python, git)"
pkg update -y >/dev/null 2>&1 || warn "pkg update had trouble — carrying on"
pkg install -y python git >/dev/null

# termux-api is the bridge to the battery, GPS and SMS. Without it Carnage
# still runs; it just has no phone skills, which rather defeats the point.
if ! pkg install -y termux-api >/dev/null 2>&1; then
    warn "termux-api package failed to install — phone skills will stay off"
fi

# ── 2. the code ─────────────────────────────────────────────────────────────
if [ -d "$CHECKOUT/.git" ]; then
    say "Updating $CHECKOUT"
    git -C "$CHECKOUT" fetch --quiet origin "$BRANCH"
    git -C "$CHECKOUT" checkout --quiet "$BRANCH"
    git -C "$CHECKOUT" pull --quiet --ff-only origin "$BRANCH"
else
    say "Cloning into $CHECKOUT"
    # Shallow: the phone wants the code, not ten years of history.
    git clone --quiet --depth 20 --branch "$BRANCH" "$REPO_URL" "$CHECKOUT"
fi

say "Installing flint-core and carnage"
pip install --quiet --upgrade pip
pip install --quiet -e "$CHECKOUT/packages/flint-core"
pip install --quiet -e "$CHECKOUT/carnage[hub]"

# ── 3. config ───────────────────────────────────────────────────────────────
mkdir -p "$STATE"
if [ -f "$CONFIG" ]; then
    say "Keeping the config already at $CONFIG"
else
    say "Writing a first config to $CONFIG"
    # A token nobody has to invent. 32 hex chars from the system CSPRNG —
    # the same secret has to go in Venom's venom.toml, and it is printed at
    # the end so it can be copied once and never thought about again.
    TOKEN="$(python -c 'import secrets; print(secrets.token_hex(16))')"
    cat > "$CONFIG" <<JSON
{
  "device": "carnage",
  "user_name": "Hrishikesh",

  "_gemini": "Paste a key here to give her a voice and web search.",
  "gemini_api_key": "",

  "hub": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8790,
    "token": "$TOKEN",
    "peers": ["venom", "flint"]
  },

  "_web": "Her face, served to this phone's own browser. Bound to loopback on purpose: http://localhost is a secure context by definition, so the browser grants GPS, the microphone and Add to Home Screen with no certificate, no flag and nothing exposed to the network.",
  "web": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8791,
    "token": "$TOKEN"
  },

  "_devices": "Her other bodies. 'can' is read out loud, so write it the way she would say it.",
  "devices": [
    {
      "name": "venom",
      "body": "the wearable on his body",
      "can": ["listen on the walk", "the earphone", "look around with the camera"]
    },
    {
      "name": "flint",
      "body": "on his desktop",
      "can": ["the screen", "his files and repos", "drive the apps on it"]
    }
  ]
}
JSON
    chmod 600 "$CONFIG"
fi

# ── 4. survive a reboot ─────────────────────────────────────────────────────
# Termux:Boot runs everything in this directory on power-on. Without it the
# hub is up until the first restart and then quietly is not, which is the
# worst of both worlds — the other devices keep trying and never say why.
BOOTDIR="$HOME/.termux/boot"
mkdir -p "$BOOTDIR"
cat > "$BOOTDIR/carnage" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/sh
# Hold a wake lock first: Android will otherwise suspend the process a few
# minutes after the screen goes off, and a hub that sleeps is not a hub.
termux-wake-lock
exec python -m carnage >> "$HOME/.carnage/carnage.log" 2>&1
BOOT
chmod +x "$BOOTDIR/carnage"

# ── 5. did it work ──────────────────────────────────────────────────────────
say "Checking"
python -m carnage --once || die "Carnage could not start — see the output above."

TOKEN="$(python - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ.get("CARNAGE_STATE", os.path.expanduser("~/.carnage"))) / "carnage.json"
print(json.loads(p.read_text()).get("hub", {}).get("token", ""))
PY
)"

ADDRS="$(python - <<'PY'
import socket

# Tailscale hands out 100.64.0.0/10. Prefer it when it is there: a LAN address
# is only right until the Pi hops onto the hotspot and the subnet changes
# under it, which on a wearable is a daily event rather than an edge case.
def tailscale(ip: str) -> bool:
    try:
        first, second = (int(part) for part in ip.split(".")[:2])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127

found = set()
try:
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith("127."):
            found.add(ip)
except OSError:
    pass

mesh = sorted(a for a in found if tailscale(a))
if mesh:
    print(mesh[0] + "   # tailscale — survives changing networks")
else:
    print(" or ".join(sorted(found)) or "<this phone's IP>")
PY
)"

cat <<DONE

  Carnage is installed — on the phone itself, so nothing else has to be
  awake for her to be.

  Start her now:      python -m carnage
  She starts herself: on every reboot, via Termux:Boot

  Open her in Chrome, then use the menu to Add to Home Screen:

    http://localhost:8791/?k=$TOKEN

  No certificate and no chrome://flags needed — a browser treats localhost
  as a secure context, so GPS, the microphone and installing to the home
  screen all just work when she is served from this phone.

  Point Venom at her — add this to /etc/venom/venom.toml, then
  'sudo systemctl restart venom':

    [sync]
    enabled = true
    device  = "venom"
    hub     = "ws://$ADDRS:8790"
    token   = "$TOKEN"

    [[device]]
    name = "carnage"
    body = "on his phone"
    can  = ["send a text", "where he actually is", "emergency SMS"]

  If the Pi moves between networks, use the phone's Tailscale name in
  place of the address above — that is the whole reason to have it.

DONE
