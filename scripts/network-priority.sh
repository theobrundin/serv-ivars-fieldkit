#!/usr/bin/env bash
#
# network-priority.sh — enforce cellular-first, ethernet-fallback default route.
#
# Some units are deployed with the PoE splitter feeding BOTH:
#   - a USB-C power lead into the Pi (power only), AND
#   - an Ethernet lead into the Pi's RJ45 port (data)
#
# When both are up simultaneously, Linux picks whichever default route has
# the lowest metric. NetworkManager's DHCP default for wired connections
# is metric 100, and cellular-connect.sh sets wwan0 to metric 50 — so
# cellular will already win in the normal case. This script is the belt
# to that suspenders: it forces eth0's metric to 700 via NetworkManager,
# and installs a dispatcher hook so the metric re-applies on every eth0
# connect event (cable replug, DHCP renew, cold-boot race).
#
# Idempotent: safe to re-run any time.
#
# Behavior:
#   - wwan0 (cellular) metric 50   -> preferred
#   - eth0  (ethernet)  metric 700  -> fallback if cellular drops
#
# If this unit is only ever going to see cellular (no PoE data uplink),
# this script does nothing harmful — eth0 just never comes up.

set -euo pipefail

ETH_METRIC=700
CELLULAR_METRIC=50
LOG_TAG="network-priority"

log() { echo "[$LOG_TAG] $*"; }

log "==== configuring network route priority (cellular > ethernet) ===="

# ---------- Ethernet: deprioritize via NetworkManager ----------
if command -v nmcli >/dev/null 2>&1; then
  ETH_CONN=$(nmcli -t -f NAME,DEVICE connection show 2>/dev/null | grep ":eth0$" | cut -d: -f1 || true)

  if [[ -n "$ETH_CONN" ]]; then
    log "found NetworkManager connection for eth0: '$ETH_CONN'"
    nmcli connection modify "$ETH_CONN" ipv4.route-metric $ETH_METRIC
    nmcli connection modify "$ETH_CONN" ipv6.route-metric $ETH_METRIC
    log "set route metric to $ETH_METRIC on '$ETH_CONN'"

    # Reapply if the connection is currently active
    if nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$ETH_CONN"; then
      nmcli connection up "$ETH_CONN" >/dev/null 2>&1 || true
    fi
  else
    log "no active NetworkManager connection for eth0 yet — dispatcher hook will apply metric on next connect"
  fi

  # Install dispatcher hook so the metric re-applies whenever eth0 goes up.
  # Handles: cable replug, DHCP lease renewal after suspend, race conditions
  # at cold boot where this script may have run before eth0 came up.
  if [[ -d /etc/NetworkManager/dispatcher.d ]]; then
    DISPATCHER_PATH="/etc/NetworkManager/dispatcher.d/90-eth0-metric"
    cat > "$DISPATCHER_PATH" <<EOF
#!/bin/bash
IFACE="\$1"
ACTION="\$2"
if [ "\$IFACE" = "eth0" ] && [ "\$ACTION" = "up" ]; then
  CONN=\$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | grep ":eth0\$" | cut -d: -f1 || true)
  if [ -n "\$CONN" ]; then
    nmcli connection modify "\$CONN" ipv4.route-metric $ETH_METRIC 2>/dev/null || true
    logger "network-priority: reapplied eth0 route metric $ETH_METRIC"
  fi
fi
EOF
    chmod +x "$DISPATCHER_PATH"
    log "installed dispatcher hook: $DISPATCHER_PATH"
  fi
else
  log "WARNING: nmcli not found — this Pi may use dhcpcd. Falling through to route-only fix."
fi

# The health supervisor alone owns cellular route metrics. An unconditional
# metric-50 safety net here would promote a failed/probationary route and
# blackhole a working Wi-Fi/Ethernet fallback during modem recovery.
log "cellular route promotion is owned exclusively by cellular-connect.service"

log "==== done ===="
log "verify with: ip route show"
log "wwan0 metric $CELLULAR_METRIC (preferred) should sit above eth0 metric $ETH_METRIC (fallback)"
