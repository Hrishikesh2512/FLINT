#!/bin/bash
# Venom stage-2 provisioning — runs on the Pi, as root, with network up.
# Started by venom-provision.service on every boot until it succeeds.
# Idempotent: safe to re-run after a partial failure (power loss, no Wi-Fi).
set -euo pipefail

# Single instance: boot unit, control channel, and manual runs must never
# overlap — concurrent pip installs corrupt the venv.
LOCK=/run/venom-provision.lock
exec 200>"$LOCK"
flock -n 200 || { echo "[venom-provision] another run is active — exiting"; exit 0; }

# Never compete with the voice loop for CPU/IO — a saturated Pi is deaf.
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ 2>/dev/null || true

REPO_URL="${VENOM_REPO_URL:-https://github.com/Hrishikesh2512/FLINT.git}"
REPO_BRANCH="${VENOM_REPO_BRANCH:-v2/rebuild}"
APP_DIR=/opt/venom/app
VENV_DIR=/opt/venom/venv
PROVISION_DIR=/opt/venom/provision
STAMP=/opt/venom/.provisioned

log() { echo "[venom-provision] $*"; }

log "starting (repo=$REPO_URL branch=$REPO_BRANCH)"

# ── 1. wait until we can actually resolve DNS (network-online can be early) ──
for i in $(seq 1 30); do
    if getent hosts github.com >/dev/null 2>&1; then break; fi
    log "waiting for DNS ($i/30)..."
    sleep 5
done
getent hosts github.com >/dev/null 2>&1 || { log "no network — will retry next boot"; exit 1; }

# ── 2. system packages (first successful run only — apt is slow) ─────────────
if [ ! -f "$STAMP" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        git python3-venv python3-pip gcc python3-dev \
        libportaudio2 alsa-utils iw \
        bluez pipewire pipewire-alsa wireplumber \
        mpv
    # Bluetooth SPA plugin: named libspa-0.2-bluetooth on Debian 12+/RPi OS;
    # older releases used libspa-0.2-bluez5. Take whichever exists.
    apt-get install -y -qq --no-install-recommends libspa-0.2-bluetooth \
        || apt-get install -y -qq --no-install-recommends libspa-0.2-bluez5
fi

# ── 3. fetch / update the repo ────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$APP_DIR" checkout -f FETCH_HEAD
else
    rm -rf "$APP_DIR"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi

# Self-update: if the repo carries a newer provisioning script, adopt it and
# re-exec so every fix pushed upstream reaches the device with zero hands.
if [ -f "$APP_DIR/venom/provisioning/provision.sh" ] \
        && ! cmp -s "$APP_DIR/venom/provisioning/provision.sh" "$0"; then
    log "provisioning script changed upstream — updating and re-executing"
    cp "$APP_DIR/venom/provisioning/provision.sh" "$0"
    chmod +x "$0"
    exec "$0"
fi

# Boot fast path: if this exact commit is already installed, there is
# nothing to do — pip alone takes minutes of full load on a Pi 4.
INSTALLED_MARK=/opt/venom/.installed-commit
HEAD_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
if [ -f "$STAMP" ] && [ "$(cat "$INSTALLED_MARK" 2>/dev/null)" = "$HEAD_COMMIT" ]; then
    log "already at $HEAD_COMMIT — nothing to install"
    exit 0
fi

# ── 4. python environment + venom package (with the voice stack) ─────────────
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"

# ── Heavy dependency install — ONLY on first run or when venom's declared
# dependencies change. A code-only "Update from GitHub" skips this entire
# block: resolving onnxruntime/numpy/scipy and rebuilding evdev from C
# source every time spiked memory and swap-thrashed the 1.8 GB Pi, stalling
# SSH and the web console mid-update. Code refresh is the cheap step below.
DEPS_HASH="$(sha256sum "$APP_DIR/venom/pyproject.toml" | cut -d' ' -f1)"
DEPS_MARK=/opt/venom/.deps-hash
if [ ! -f "$STAMP" ] || [ "$(cat "$DEPS_MARK" 2>/dev/null)" != "$DEPS_HASH" ]; then
    log "installing dependencies (first run or deps changed)"

    "$VENV_DIR/bin/pip" install --quiet --upgrade "$APP_DIR/packages/flint-core"
    # openwakeword declares tflite-runtime on Linux, which has no wheels for
    # modern Python (3.13 on current RPi OS). Venom uses its ONNX path, so
    # install it without dependency resolution and supply the real ones.
    "$VENV_DIR/bin/pip" install --quiet --no-deps "openwakeword>=0.6"
    "$VENV_DIR/bin/pip" install --quiet "onnxruntime>=1.20,<1.22" "numpy>=1.26,<2.5" \
        "tqdm>=4.64" "scipy>=1.11" "requests>=2.31"

    # Venom never trains custom verifier models, and that (unused) corner of
    # openwakeword drags in scikit-learn, which won't import on this fresh
    # Python/OS combination. Make the import optional instead of fighting it.
    "$VENV_DIR/bin/python" - <<'PYEOF'
from pathlib import Path
import sys
init = next(Path(sys.prefix, "lib").glob("python3.*/site-packages/openwakeword/__init__.py"))
text = init.read_text()
target = "from openwakeword.custom_verifier_model import train_custom_verifier"
if target in text and "except Exception" not in text.split(target)[1][:60]:
    text = text.replace(
        target,
        "try:\n    " + target + "\nexcept Exception:\n    train_custom_verifier = None",
    )
    init.write_text(text)
    print("[venom-provision] openwakeword: custom-verifier import made optional")
else:
    print("[venom-provision] openwakeword: import already optional")
PYEOF

    "$VENV_DIR/bin/pip" install --quiet --upgrade "$APP_DIR/venom[voice]"
    "$VENV_DIR/bin/pip" install --quiet --upgrade yt-dlp evdev

    # Self-heal: a power cut mid-install can leave corrupted native wheels
    # (observed on real hardware: numpy Bus error after an unclean reboot).
    if ! "$VENV_DIR/bin/python" -c "import numpy, scipy, onnxruntime, openwakeword" 2>/dev/null; then
        log "native libraries broken — force-reinstalling"
        "$VENV_DIR/bin/pip" install --quiet --force-reinstall --no-cache-dir \
            "numpy>=1.26,<2.5" "scipy>=1.11"
        "$VENV_DIR/bin/python" -c "import numpy, scipy, onnxruntime, openwakeword" \
            || { log "libraries still broken after reinstall — will retry next boot"; exit 1; }
        log "native libraries repaired"
    fi

    # Wake word models: staged copies on the boot partition, else download.
    OWW_DST="$("$VENV_DIR/bin/python" -c 'import openwakeword, pathlib; print(pathlib.Path(openwakeword.__file__).parent / "resources" / "models")' 2>/dev/null || true)"
    if [ -n "$OWW_DST" ]; then
        mkdir -p "$OWW_DST"
        if [ -d /boot/firmware/venom/oww-models ]; then
            cp -n /boot/firmware/venom/oww-models/*.onnx "$OWW_DST"/ 2>/dev/null || true
            log "wake word models staged from boot partition"
        fi
    fi
    "$VENV_DIR/bin/python" - <<'PYEOF'
import openwakeword.utils
openwakeword.utils.download_models(["hey_jarvis"])
print("[venom-provision] wake word model ready")
PYEOF
    echo "$DEPS_HASH" > "$DEPS_MARK"
else
    log "dependencies unchanged — skipping heavy install"
fi

# Always refresh our own code (pure Python: no build, no dep resolution,
# a few hundred KB). Versions stay constant across commits, so
# --force-reinstall --no-deps is what makes a pushed fix actually land.
"$VENV_DIR/bin/pip" install --quiet --force-reinstall --no-deps \
    "$APP_DIR/packages/flint-core" "$APP_DIR/venom"

# ── 5. service account + config ───────────────────────────────────────────────
# Service user is "venom" (the console terminal's `whoami` shows it).
# Migrate a legacy "venomd" account in place so existing file ownership
# and group memberships carry over without orphaning anything. usermod -l
# refuses while the account owns live processes, so stop its services
# first (section 6 restarts them under the new name).
if id venomd >/dev/null 2>&1 && ! id venom >/dev/null 2>&1; then
    log "renaming service user venomd -> venom"
    systemctl stop venom.service pipewire-system.service \
        wireplumber-system.service 2>/dev/null || true
    pkill -KILL -u venomd 2>/dev/null || true
    sleep 1
    usermod -l venom venomd
    groupmod -n venom venomd 2>/dev/null || true
fi
id venom >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin venom
usermod -aG audio venom
usermod -aG bluetooth venom || true
usermod -aG input venom || true  # headset AVRCP buttons
usermod -aG systemd-journal venom || true  # web console log viewer

mkdir -p /etc/venom
if [ ! -f /etc/venom/venom.toml ]; then
    if [ -f "$PROVISION_DIR/venom.toml" ]; then
        install -m 0640 -g venom "$PROVISION_DIR/venom.toml" /etc/venom/venom.toml
    else
        install -m 0640 -g venom "$APP_DIR/venom/provisioning/venom.toml" /etc/venom/venom.toml
    fi
else
    # keep existing config but tighten perms (it holds the API key)
    chgrp venom /etc/venom/venom.toml && chmod 0640 /etc/venom/venom.toml
fi
# Old files may still be owned by the pre-rename account name.
chown -R venom:venom /var/lib/venom 2>/dev/null || true

# Web console access PIN: generate once, persist in the state dir. It
# survives config rewrites; read it over SSH with:
#   ssh <user>@venom.local  then  sudo cat /var/lib/venom/web_token
mkdir -p /var/lib/venom
if [ ! -s /var/lib/venom/web_token ]; then
    (openssl rand -hex 4 2>/dev/null \
        || tr -dc 'a-f0-9' </dev/urandom | head -c 8) > /var/lib/venom/web_token
fi
chown venom:venom /var/lib/venom/web_token 2>/dev/null || true
chmod 640 /var/lib/venom/web_token
log "web console PIN: $(cat /var/lib/venom/web_token)"

# Fresh RPi OS ships Bluetooth soft-blocked — unblock it or nothing pairs.
rfkill unblock bluetooth 2>/dev/null || true

# WirePlumber gates Bluetooth on an active login seat by default; an
# appliance has none. Install the override before starting the services.
mkdir -p /etc/wireplumber/wireplumber.conf.d
install -m 0644 "$APP_DIR/venom/provisioning/80-venom-bluetooth.conf" \
    /etc/wireplumber/wireplumber.conf.d/80-venom-bluetooth.conf

# BlueZ's D-Bus policy reserves HFP profile registration for root; grant it
# to the service user or the headset microphone never exists.
install -m 0644 "$APP_DIR/venom/provisioning/venom-dbus-bluetooth.conf" \
    /etc/dbus-1/system.d/venom-dbus-bluetooth.conf
systemctl reload dbus 2>/dev/null || true

# Per-user PipeWire instances (spawned by any SSH login) fight the system
# ones for the Bluetooth headset — mask them everywhere.
systemctl --global mask pipewire.socket pipewire.service wireplumber.service \
    pipewire-pulse.socket pipewire-pulse.service 2>/dev/null || true
pkill -u "$(id -nu 1000 2>/dev/null || echo nobody)" -f 'pipewire|wireplumber' 2>/dev/null || true

# ── 6. install + start the services ───────────────────────────────────────────
# System-wide PipeWire/WirePlumber: Bluetooth audio with no user session.
install -m 0644 "$APP_DIR/venom/provisioning/pipewire-system.service" \
    /etc/systemd/system/pipewire-system.service
install -m 0644 "$APP_DIR/venom/provisioning/wireplumber-system.service" \
    /etc/systemd/system/wireplumber-system.service
install -m 0644 "$APP_DIR/venom/provisioning/venom.service" /etc/systemd/system/venom.service
# Root control channel: the web console writes /run/venom/control.request,
# this path unit dispatches update/restart/reboot with root rights.
install -m 0755 "$APP_DIR/venom/provisioning/control.sh" /opt/venom/provision/control.sh
install -m 0644 "$APP_DIR/venom/provisioning/venom-control.path" \
    /etc/systemd/system/venom-control.path
install -m 0644 "$APP_DIR/venom/provisioning/venom-control.service" \
    /etc/systemd/system/venom-control.service
# Root shell daemon: gives the web console terminal a full-privilege shell
# (the daemon's own terminal is sealed by ProtectSystem=strict). Runs as
# root outside the sandbox; socket is group-venom so only the console reaches it.
install -m 0644 "$APP_DIR/venom/provisioning/venom-shell.service" \
    /etc/systemd/system/venom-shell.service
systemctl daemon-reload
systemctl enable --now venom-control.path
# Non-critical add-on: never let a shell-daemon hiccup abort provisioning and
# leave the voice service un-restarted. On failure the console just falls back
# to its sandboxed in-process shell.
systemctl enable --now venom-shell.service || true
systemctl enable bluetooth.service pipewire-system.service wireplumber-system.service
systemctl restart bluetooth.service pipewire-system.service wireplumber-system.service
systemctl enable venom.service
systemctl restart venom.service

# ── 6b. WhatsApp bridge (self-hosted Baileys) ────────────────────────────────
# A small Node service that owns the WhatsApp Web session so Venom can send
# messages by voice; incoming messages are forwarded to the ntfy notify topic.
# Kept OUTSIDE the git checkout (/opt/venom/whatsapp-service) so node_modules
# survives `git checkout -f`. Node deps reinstall only when package.json changes.
WA_SRC="$APP_DIR/venom/whatsapp-service"
WA_DST=/opt/venom/whatsapp-service
if [ -d "$WA_SRC" ]; then
    # NB: compute the version in an `if` guard, not `VAR=$(node -v)` — under
    # `set -e` a missing `node` (127) inside a command substitution aborts the
    # whole script before we ever install it.
    NODE_MAJOR=""
    if command -v node >/dev/null 2>&1; then
        NODE_MAJOR="$(node -v | sed 's/v\([0-9]*\).*/\1/')"
    fi
    if [ -z "$NODE_MAJOR" ] || [ "$NODE_MAJOR" -lt 18 ]; then
        log "installing Node.js 20 (NodeSource)"
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 || true
        apt-get install -y -qq nodejs || log "node install failed — WhatsApp bridge skipped"
    fi
    if command -v node >/dev/null 2>&1; then
        mkdir -p "$WA_DST"
        cp -f "$WA_SRC/package.json" "$WA_SRC/index.js" "$WA_DST/"
        [ -f "$WA_SRC/README.md" ] && cp -f "$WA_SRC/README.md" "$WA_DST/"
        WA_HASH="$(sha256sum "$WA_DST/package.json" | cut -d' ' -f1)"
        if [ "$(cat "$WA_DST/.deps-hash" 2>/dev/null)" != "$WA_HASH" ]; then
            log "installing WhatsApp bridge npm deps (first run or deps changed)"
            (cd "$WA_DST" && npm install --omit=dev --no-audit --no-fund) \
                && echo "$WA_HASH" > "$WA_DST/.deps-hash" \
                || log "npm install failed — WhatsApp bridge may not start"
        fi
        mkdir -p /var/lib/venom/whatsapp
        chown -R venom:venom "$WA_DST" /var/lib/venom/whatsapp

        # Forward incoming WhatsApp to the same ntfy topic Venom already reads.
        WA_TOPIC="$(sed -n 's/^[[:space:]]*notify_topic[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
            /etc/venom/venom.toml 2>/dev/null | head -1)"
        printf 'NTFY_TOPIC=%s\n' "$WA_TOPIC" > /etc/venom/whatsapp.env
        chgrp venom /etc/venom/whatsapp.env && chmod 0640 /etc/venom/whatsapp.env

        install -m 0644 "$APP_DIR/venom/provisioning/venom-whatsapp.service" \
            /etc/systemd/system/venom-whatsapp.service
        systemctl daemon-reload
        systemctl enable --now venom-whatsapp.service || true
        log "WhatsApp bridge up — pair it by scanning the QR:"
        log "  journalctl -u venom-whatsapp -n 40 --no-pager   (WhatsApp → Linked Devices)"
    fi
fi

# ── 7. appliance niceties for a battery-powered headless box ─────────────────
# Keep journald small and RAM-first (the whole OS lives on the pendrive).
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/venom.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
EOF
systemctl restart systemd-journald || true

# HDMI stays unused on a wearable — don't waste battery on it.
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_blanking 1 || true
fi

# Wi-Fi power save costs 100–600 ms latency spikes on every packet and
# starves Bluetooth on the Pi's shared radio — always off for a voice
# wearable (measured: ping avg dropped from ~350 ms to <10 ms).
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/venom-wifi.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
iw dev wlan0 set power_save off 2>/dev/null || true

# Prefer IPv4: phone hotspots routinely advertise broken IPv6, which stalls
# DNS/HTTPS (and music) for seconds before timing out to v4. Tell glibc to
# try A records first everywhere.
if ! grep -q '^precedence ::ffff:0:0/96  100' /etc/gai.conf 2>/dev/null; then
    echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf
fi

# ── FIXED (Fix 3): DNS resilience — kill the boot-time [Errno -3] storm ───────
# Venom used to start before DNS was ready and spend ~40s failing to resolve
# generativelanguage.googleapis.com. Two defences, both idempotent:
#
#   1. A local caching resolver (dnsmasq, cache-size=1000, Google upstreams) so
#      lookups are answered from RAM within milliseconds after the first hit and
#      survive brief upstream hiccups.
#   2. A hardcoded /etc/hosts fallback for the Gemini endpoint, refreshed each
#      successful provision, so name resolution works even before dnsmasq/Wi-Fi
#      DNS is fully up.
if ! command -v dnsmasq >/dev/null 2>&1; then
    apt-get install -y -qq --no-install-recommends dnsmasq || true
fi
if command -v dnsmasq >/dev/null 2>&1; then
    mkdir -p /etc/dnsmasq.d
    cat > /etc/dnsmasq.d/venom.conf <<'EOF'
# Venom local DNS cache — fast, resilient name resolution for the voice loop.
cache-size=1000
server=8.8.8.8
server=8.8.4.4
# Don't read /etc/resolv.conf (it may point back at us); use the servers above.
no-resolv
# Bind only to loopback: this cache is for the appliance itself.
listen-address=127.0.0.1
bind-interfaces
EOF
    systemctl enable dnsmasq 2>/dev/null || true
    systemctl restart dnsmasq 2>/dev/null || true
    # Point the system resolver at the local cache (NetworkManager-friendly).
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/venom-dns.conf <<'EOF'
[main]
dns=default
[global-dns-domain-*]
servers=127.0.0.1
EOF
fi

# /etc/hosts fallback: resolve the Gemini endpoint now (DNS is up — we cloned
# the repo above) and pin the IP so a cold-boot lookup never blocks Venom.
GEMINI_HOST=generativelanguage.googleapis.com
GEMINI_IP="$(getent ahostsv4 "$GEMINI_HOST" 2>/dev/null | awk 'NR==1{print $1}')"
if [ -n "$GEMINI_IP" ]; then
    # Remove any stale line we previously wrote, then append the fresh one.
    sed -i "/[[:space:]]$GEMINI_HOST\$/d" /etc/hosts 2>/dev/null || true
    echo "$GEMINI_IP $GEMINI_HOST" >> /etc/hosts
    log "pinned $GEMINI_HOST -> $GEMINI_IP in /etc/hosts (DNS fallback)"
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP"
echo "$HEAD_COMMIT" > "$INSTALLED_MARK"

# Stay enabled: this script re-runs on every boot as the update channel —
# git fetch + pip install of local paths is seconds when nothing changed,
# and every fix pushed upstream reaches the device hands-free.
install -m 0644 "$APP_DIR/venom/provisioning/venom-provision.service" \
    /etc/systemd/system/venom-provision.service
systemctl daemon-reload
systemctl enable venom-provision.service || true
log "done — venom.service is running. Status: /run/venom/status.json"
