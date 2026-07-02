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

# `systemctl enable` may not work inside imager's firstrun environment —
# create the WantedBy symlink directly, which is all enable does.
mkdir -p /etc/systemd/system/multi-user.target.wants
ln -sf /etc/systemd/system/venom-provision.service \
       /etc/systemd/system/multi-user.target.wants/venom-provision.service

echo "[venom] firstboot hook done — provisioning will run on next boot"
