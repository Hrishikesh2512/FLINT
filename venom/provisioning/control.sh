#!/bin/bash
# Venom control dispatcher — runs as root when the (unprivileged) daemon
# drops a request file. The web console's only path to privileged actions.
set -u
REQ=/run/venom/control.request
[ -f "$REQ" ] || exit 0
CMD=$(head -c 64 "$REQ" | tr -cd 'a-z-')
rm -f "$REQ"
echo "[venom-control] request: $CMD"
case "$CMD" in
    update)   exec /opt/venom/provision/provision.sh ;;
    restart)  exec systemctl restart venom.service ;;
    reboot)   exec /sbin/reboot ;;
    poweroff) exec /sbin/poweroff ;;   # clean shutdown: flush + unmount
    *)        echo "[venom-control] unknown request: $CMD" ;;
esac
