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

# Forget unwanted Wi-Fi networks: one SSID per line in venom/forget-wifi.txt.
# Needed when a network configured at flash time turns out to be hostile
# (e.g. guest networks with client isolation) — deletes every NM profile
# that references it, including stale cloud-init renders.
if [ -f "$SRC"/forget-wifi.txt ]; then
    while IFS= read -r ssid; do
        [ -z "$ssid" ] && continue
        grep -l "$ssid" /etc/NetworkManager/system-connections/* 2>/dev/null \
            | while IFS= read -r conn; do
                rm -f "$conn"
                echo "[venom] forgot Wi-Fi profile: $conn (matched '$ssid')"
            done
    done < "$SRC"/forget-wifi.txt
    if command -v nmcli >/dev/null 2>&1; then
        nmcli connection reload 2>/dev/null || true
        # Deleting a profile does not drop an already-active session — bounce
        # the Wi-Fi device so it re-picks from the surviving profiles.
        while IFS= read -r ssid; do
            [ -z "$ssid" ] && continue
            if nmcli -t -f active,ssid dev wifi 2>/dev/null | grep -q "^yes:$ssid$"; then
                nmcli device disconnect wlan0 2>/dev/null || true
                sleep 2
                nmcli device connect wlan0 2>/dev/null || true
                break
            fi
        done < "$SRC"/forget-wifi.txt
    fi
fi

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

# Under cloud-init runcmd systemd is fully up and the network is already
# connected — kick provisioning off right now instead of waiting a reboot.
# Under legacy firstrun this fails harmlessly and the next boot handles it.
if systemctl daemon-reload 2>/dev/null && systemctl start --no-block venom-provision.service 2>/dev/null; then
    echo "[venom] firstboot hook done — provisioning started"
else
    echo "[venom] firstboot hook done — provisioning will run on next boot"
fi

# Black box: write a boot report onto the FAT partition so the appliance can
# be diagnosed from any laptop just by plugging the drive in — no network,
# no SSH, no screen needed.
{
    echo "==== venom boot report: $(date -Is) ===="
    echo "-- wifi --"
    nmcli -t -f DEVICE,STATE,CONNECTION device 2>/dev/null || echo "nmcli unavailable"
    nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi 2>/dev/null | head -8
    echo "-- profiles --"
    ls /etc/NetworkManager/system-connections/ 2>/dev/null
    echo "-- addresses --"
    ip -brief addr 2>/dev/null
    echo "-- provision --"
    systemctl is-active venom-provision venom 2>/dev/null
    journalctl -u venom-provision -n 8 --no-pager 2>/dev/null | tail -8
    echo "-- firstboot log tail --"
    tail -12 /var/log/venom-firstboot.log 2>/dev/null
} >> "$SRC"/boot-report.txt 2>&1 || true
