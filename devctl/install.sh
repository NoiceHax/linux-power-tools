#!/bin/bash
# Usage: sudo ./install.sh        (turns webcam, card reader, bluetooth OFF and keeps them off across reboots)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
USER_NAME="${SUDO_USER:-minty}"
HERE="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "$HERE/devctl.py" /usr/local/bin/devctl
mkdir -p /etc/devctl /var/lib/devctl
if [ ! -s /etc/devctl/token ]; then
  head -c 24 /dev/urandom | base64 | tr -d '=+/\n' > /etc/devctl/token
fi
chown root:"$USER_NAME" /etc/devctl/token && chmod 640 /etc/devctl/token   # readable by you only (plus root)

cat > /etc/systemd/system/devctl.service <<'EOF'
[Unit]
Description=devctl - loopback-only USB power switch
After=multi-user.target

[Service]
ExecStart=/usr/local/bin/devctl serve
Restart=on-failure
# the daemon only needs to write USB sysfs and bind loopback
ProtectHome=true
PrivateTmp=true
RestrictAddressFamilies=AF_INET AF_UNIX
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now devctl.service
sleep 1
sudo -u "$USER_NAME" devctl status
echo
echo "Done. Devices are OFF. Turn them on with:  devctl tui   (or: devctl on all)"
echo "Listening on 127.0.0.1:8765 only. Remote: ssh -L 8765:127.0.0.1:8765 $USER_NAME@<host>"
