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

# ── 4. python environment + venom package (with the voice stack) ─────────────
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
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
# If the core imports crash, force-reinstall them fresh and re-verify.
# A wheelhouse on the boot partition (staged by prepare-pendrive) is used
# first so the repair works even on a bad connection.
if ! "$VENV_DIR/bin/python" -c "import numpy, scipy, onnxruntime, openwakeword" 2>/dev/null; then
    log "native libraries broken — force-reinstalling"
    "$VENV_DIR/bin/pip" install --quiet --force-reinstall --no-cache-dir \
        "numpy>=1.26,<2.5" "scipy>=1.11"
    "$VENV_DIR/bin/python" -c "import numpy, scipy, onnxruntime, openwakeword" \
        || { log "libraries still broken after reinstall — will retry next boot"; exit 1; }
    log "native libraries repaired"
fi

# Wake word models: use copies staged on the boot partition when present,
# else download once. (Resolve the package dir via Python — a bare ls glob
# exits non-zero when the fresh install lacks the models dir, killing the
# script under set -e.)
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

# ── 5. service account + config ───────────────────────────────────────────────
id venomd >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin venomd
usermod -aG audio venomd
usermod -aG bluetooth venomd || true
usermod -aG input venomd || true  # headset AVRCP buttons
usermod -aG systemd-journal venomd || true  # web console log viewer

mkdir -p /etc/venom
if [ ! -f /etc/venom/venom.toml ]; then
    if [ -f "$PROVISION_DIR/venom.toml" ]; then
        install -m 0640 -g venomd "$PROVISION_DIR/venom.toml" /etc/venom/venom.toml
    else
        install -m 0640 -g venomd "$APP_DIR/venom/provisioning/venom.toml" /etc/venom/venom.toml
    fi
else
    # keep existing config but tighten perms (it holds the API key)
    chgrp venomd /etc/venom/venom.toml && chmod 0640 /etc/venom/venom.toml
fi

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
systemctl daemon-reload
systemctl enable --now venom-control.path
systemctl enable bluetooth.service pipewire-system.service wireplumber-system.service
systemctl restart bluetooth.service pipewire-system.service wireplumber-system.service
systemctl enable venom.service
systemctl restart venom.service

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

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP"

# Stay enabled: this script re-runs on every boot as the update channel —
# git fetch + pip install of local paths is seconds when nothing changed,
# and every fix pushed upstream reaches the device hands-free.
install -m 0644 "$APP_DIR/venom/provisioning/venom-provision.service" \
    /etc/systemd/system/venom-provision.service
systemctl daemon-reload
systemctl enable venom-provision.service || true
log "done — venom.service is running. Status: /run/venom/status.json"
