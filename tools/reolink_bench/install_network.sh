#!/bin/bash
# Persistent isolated camera network for the verified home bench Pi only.
# Usage: sudo bash install_network.sh --check|--apply|--rollback
set -euo pipefail
MODE=${1:---check}
NAME=serv-reolink-camera
STATE=/var/lib/serv-reolink-network
UNIT=/etc/systemd/system/serv-reolink-dhcp.service
[[ $(hostname | tr '[:upper:]' '[:lower:]') == servtest ]] || { echo 'Refusing non-bench host'; exit 1; }
[[ $EUID == 0 ]] || { echo 'Administrator access required'; exit 1; }
[[ $MODE == --check || $MODE == --apply || $MODE == --rollback ]] || exit 2
umask 077
if [[ $MODE == --rollback ]]; then
    [[ -f "$STATE/owned-profile-uuid" ]] || { echo 'No owned installation'; exit 0; }
    systemctl disable --now serv-reolink-dhcp.service
    nmcli connection delete uuid "$(cat "$STATE/owned-profile-uuid")"
    rm -f "$UNIT" "$STATE/owned-profile-uuid"
    systemctl daemon-reload
    nmcli -w 10 connection up uuid 75a1216a-9d1a-30cd-8aca-ace5526ec021 ifname eth0 || true
    echo 'Camera network removed; original Ethernet DHCP profile restored.'
    exit 0
fi
ip -4 address show dev wlan0 | grep -q 'inet 192.168.39.' || { echo 'Independent Wi-Fi management required'; exit 1; }
ip -4 route show default | grep -q 'dev wlan0' || { echo 'Wi-Fi default route required'; exit 1; }
[[ $(cat /sys/class/net/eth0/carrier) == 1 ]] || { echo 'Camera Ethernet link absent'; exit 1; }
python3 - <<'PY'
import ipaddress,json,subprocess
camera=ipaddress.ip_network('192.168.50.0/24')
for route in json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','all'])):
    destination=route.get('dst','default')
    if destination=='default' or route.get('dev')=='eth0': continue
    if camera.overlaps(ipaddress.ip_network(destination,strict=False)):
        raise SystemExit('Camera subnet conflicts with a non-camera interface')
PY
if [[ $MODE == --check ]]; then
    echo 'Ready: eth0 camera subnet 192.168.50.0/24, Wi-Fi remains default.'
    exit 0
fi
install -d -m 700 "$STATE" /etc/serv-reolink
if [[ -f "$STATE/owned-profile-uuid" ]]; then
    nmcli -g connection.id connection show uuid "$(cat "$STATE/owned-profile-uuid")" | grep -qx "$NAME"
else
    if nmcli -g NAME connection show | grep -qx "$NAME"; then
        echo 'Refusing to adopt an unrecorded existing camera profile'; exit 1
    fi
    ip -j address > "$STATE/addresses.before.json"
    ip -j route show table all > "$STATE/routes.before.json"
    nmcli -f connection,ipv4,ipv6 connection show netplan-eth0 > "$STATE/original-ethernet.txt"
    nmcli connection add type ethernet ifname eth0 con-name "$NAME" \
        autoconnect yes connection.autoconnect-priority 100 \
        ipv4.method manual ipv4.addresses 192.168.50.1/24 \
        ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled
    nmcli -g connection.uuid connection show "$NAME" > "$STATE/owned-profile-uuid"
fi
cat > /etc/serv-reolink/udhcpd.conf <<'EOF'
start 192.168.50.2
end 192.168.50.2
interface eth0
max_leases 1
remaining yes
auto_time 30
lease_file /var/lib/serv-reolink-network/udhcpd.leases
pidfile /run/serv-reolink-network/udhcpd.pid
opt subnet 255.255.255.0
opt lease 3600
static_lease ec:71:db:a6:d3:f2 192.168.50.2
EOF
cat > "$UNIT" <<'EOF'
[Unit]
Description=Reolink bench camera DHCP on isolated Ethernet
After=NetworkManager.service
Requires=NetworkManager.service
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStartPre=/usr/bin/timeout 30 /bin/sh -c 'until /usr/sbin/ip -4 address show dev eth0 | /usr/bin/grep -q "inet 192.168.50.1/24"; do sleep 1; done'
ExecStart=/usr/bin/busybox udhcpd -f /etc/serv-reolink/udhcpd.conf
Restart=on-failure
RestartSec=5
RuntimeDirectory=serv-reolink-network
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/serv-reolink-network
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_RAW
RestrictAddressFamilies=AF_INET AF_PACKET AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemd-analyze verify "$UNIT"
nmcli -w 10 connection up uuid "$(cat "$STATE/owned-profile-uuid")" ifname eth0
systemctl stop serv-reolink-camera-dhcp.service 2>/dev/null || true
systemctl stop serv-reolink-bench-dhcp.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable serv-reolink-dhcp.service
systemctl restart serv-reolink-dhcp.service
systemctl is-active --quiet serv-reolink-dhcp.service
echo 'Persistent camera Ethernet and DHCP installed; no gateway/DNS advertised.'
