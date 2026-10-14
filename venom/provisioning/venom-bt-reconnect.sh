#!/bin/bash
# Keep Venom's Bluetooth headset connected. If the configured headset has
# dropped (single-point earbuds wander off to a phone, the Pi's flaky radio
# blips, etc.), grab it back. Safe to run every ~30s from a systemd timer.
# The headset is [audio] bluetooth_mac in /etc/venom/venom.toml; VENOM_BT_MAC
# overrides it.
MAC="${VENOM_BT_MAC:-$(sed -n 's/^[[:space:]]*bluetooth_mac[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    /etc/venom/venom.toml 2>/dev/null | head -1)}"
[ -n "$MAC" ] || exit 0                                    # no headset configured
info="$(bluetoothctl info "$MAC" 2>/dev/null)"
echo "$info" | grep -q "Connected: yes" && exit 0          # already up
echo "$info" | grep -q "Paired: yes" || exit 0             # not paired -> nothing to do
timeout 20 bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
