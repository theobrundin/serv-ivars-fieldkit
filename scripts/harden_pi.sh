#!/usr/bin/env bash
# Idempotent host hardening for a headless Serv field camera.
# Password SSH stays enabled unless --key-only is explicitly supplied. Apply
# that final switch only after a separate key-authenticated session succeeds.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_MODE="password-fallback"
if [[ "${1:-}" == "--key-only" ]]; then
  SSH_MODE="key-only"
elif [[ -n "${1:-}" ]]; then
  echo "usage: sudo $0 [--key-only]" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

install -d -m 755 -o root -g root /etc/serv /etc/ssh/sshd_config.d
install -m 600 -o root -g root \
  "$REPO_DIR/systemd/serv-firewall.nft" /etc/serv/serv-firewall.nft
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-firewall.service" \
  /etc/systemd/system/serv-firewall.service

password_auth=yes
if [[ "$SSH_MODE" == "key-only" ]]; then
  if [[ ! -s /home/serv/.ssh/authorized_keys ]]; then
    echo "refusing key-only SSH: /home/serv/.ssh/authorized_keys is empty" >&2
    exit 1
  fi
  password_auth=no
fi

ssh_tmp="$(mktemp)"
cat > "$ssh_tmp" <<EOF
# Managed by Serv hardening. Preserve TCP forwarding for loopback camera
# preview tunnels, but disable remote forwarding and credential forwarding.
PermitRootLogin no
PasswordAuthentication $password_auth
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
MaxAuthTries 3
MaxStartups 3:30:10
LoginGraceTime 30
AllowUsers serv
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding local
AllowStreamLocalForwarding no
GatewayPorts no
PermitTunnel no
PermitUserEnvironment no
PermitUserRC no
ClientAliveInterval 120
ClientAliveCountMax 2
LogLevel VERBOSE
EOF
install -m 600 -o root -g root "$ssh_tmp" \
  /etc/ssh/sshd_config.d/00-serv-hardening.conf
rm -f "$ssh_tmp"
# OpenSSH uses the first value found. Cloud images commonly ship an earlier
# 50-cloud-init.conf with PasswordAuthentication yes, which defeated 60-serv.
rm -f /etc/ssh/sshd_config.d/60-serv-hardening.conf
sshd -t
effective_password_auth="$(sshd -T | awk '$1 == "passwordauthentication" {print $2}')"
if [[ "$effective_password_auth" != "$password_auth" ]]; then
  echo "SSH authentication precedence differs; review the earlier configuration" >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now serv-firewall.service
systemctl reload ssh.service

# A headless field unit does not need LAN discovery, Bluetooth, a desktop
# login manager, RPC/NFS port mapping, or Raspberry Pi Connect.
for unit in avahi-daemon.service avahi-daemon.socket bluetooth.service \
            lightdm.service rpcbind.service rpcbind.socket \
            rpi-connect.service rpi-connect-wayvnc.service; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
    systemctl disable --now "$unit" 2>/dev/null || true
  fi
done

# Raspberry Pi OS images may retain a passwordless tty1 auto-login even after
# the desktop login manager is disabled. That is a direct physical-console
# bypass on a field enclosure. Preserve the drop-in for rollback, then remove
# it. The normal password-protected getty takes effect on the next restart or
# boot (the installer may be running from the current console, so do not kill
# that session here).
autologin_dropin=/etc/systemd/system/getty@tty1.service.d/autologin.conf
if [[ -f "$autologin_dropin" ]]; then
  install -d -m 700 -o serv -g serv /home/serv/field-backups
  install -m 600 -o root -g root "$autologin_dropin" \
    /home/serv/field-backups/console-autologin.conf.disabled
  rm -f "$autologin_dropin"
  rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true
  systemctl daemon-reload
fi

chmod 600 /home/serv/plate_reader/config.json 2>/dev/null || true
chmod 600 /home/serv/plate_reader/queue.db* 2>/dev/null || true
chmod 600 /home/serv/plate_reader/zone_preview.jpg 2>/dev/null || true

echo "hardening applied; ssh_mode=$SSH_MODE"
