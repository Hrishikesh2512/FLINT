#!/bin/bash
# Venom stage-1 hook — appended to Raspberry Pi Imager's firstrun.sh by
# prepare-pendrive.ps1. Runs as root on the very first boot, BEFORE the
# network/user setup has finished, so it does the minimum possible:
# copy the provisioning payload into the rootfs and enable the stage-2
# service, which does the real work on the next boot (with network).
set -u

# The payload directory sits next to this script on the boot (FAT) partition.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p /opt/venom/provision
cp -f "$SRC"/provision.sh        /opt/venom/provision/provision.sh
cp -f "$SRC"/venom.service       /opt/venom/provision/venom.service
cp -f "$SRC"/venom-provision.service /etc/systemd/system/venom-provision.service
[ -f "$SRC"/venom.toml ] && cp -f "$SRC"/venom.toml /opt/venom/provision/venom.toml
chmod +x /opt/venom/provision/provision.sh

# Extra Wi-Fi networks (phone hotspot etc.) — NetworkManager keyfiles so the
# Pi hops between home Wi-Fi and the hotspot automatically, no cables ever.
if [ -f "$SRC"/extra-wifi.tsv ]; then
    mkdir -p /etc/NetworkManager/system-connections
    priority=50
    while IFS="$(printf '\t')" read -r ssid password; do
        [ -z "$ssid" ] && continue
        conn="/etc/NetworkManager/system-connections/venom-${ssid}.nmconnection"
        cat > "$conn" <<NMEOF
[connection]
id=${ssid}
type=wifi
autoconnect=true
autoconnect-priority=${priority}

[wifi]
mode=infrastructure
ssid=${ssid}

[wifi-security]
key-mgmt=wpa-psk
psk=${password}

[ipv4]
method=auto

[ipv6]
method=auto
NMEOF
        chmod 600 "$conn"
        priority=$((priority - 1))
        echo "[venom] added Wi-Fi network: ${ssid}"
    done < "$SRC"/extra-wifi.tsv
fi

# `systemctl enable` may not work inside imager's firstrun environment —
# create the WantedBy symlink directly, which is all enable does.
mkdir -p /etc/systemd/system/multi-user.target.wants
ln -sf /etc/systemd/system/venom-provision.service \
       /etc/systemd/system/multi-user.target.wants/venom-provision.service

echo "[venom] firstboot hook done — provisioning will run on next boot"
