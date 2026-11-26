#!/bin/sh
# Reset the pod's network namespace before the wireguard sidecar's own init runs.
#
# Runs as the sidecar container's entrypoint wrapper, on EVERY start of that container. The
# first start of a pod finds nothing to remove. A restart within a running pod finds the
# previous container's tunnel state, because the netns belongs to the pod and outlives the
# container: the wg0 link, wg-quick's policy-routing rules, the LAN routes the Mullvad mod's
# PostUp added, and the kill-switch REJECT in the OUTPUT chain. A start whose tunnel failed also
# leaves the netns without its default route, which the image's own init deleted; the reset puts
# it back. Left in place, that state is
# self-sustaining twice over — the mod's api.mullvad.net call routes into the dead wg0 and
# fails, and even when it succeeds, PostUp's `ip route add` hits the routes that already exist
# and wg-quick tears the tunnel back down (see this role's CLAUDE.md, "A VPN kill-switch
# outlives the container it fenced": cleared only by a pod delete). This script is what makes a
# container restart equivalent to that pod delete.
#
# The order matters: the gate goes up BEFORE the stale tunnel comes down. qbittorrent keeps
# running in the pod while the sidecar restarts, and between the reset and the mod's PostUp
# the netns has no tunnel and no kill-switch. The uid-scoped REJECT installed first covers
# that window: every non-LAN packet from qbittorrent's uid that is not leaving via wg0 is
# refused, while root's — the mod's API calls — is not. The mod's own mark-exempt REJECT
# lands after it once the tunnel is up. Both rules stay; they agree wherever they overlap.
#
# The gate exempts fwmark 51820 as well as `-o wg0`, and the exemption is load-bearing. A
# packet qbittorrent sends into wg0 leaves the pod as an encrypted UDP carrier on eth0, and
# that carrier still belongs to the originating socket — so `--uid-owner` matches it and
# `! -o wg0` is true. Without the mark exemption the gate rejects every carrier packet for
# uid 1000: the tunnel is up, root reaches the internet through it, and
# qbittorrent sees only timeouts (DHT 0 nodes, every peer and tracker dead). wg-quick sets
# the mark (`wg set wg0 fwmark 51820`) and the mod's own REJECT exempts it the same way.
#
# Fails closed. If the gate cannot be installed, the script exits non-zero and the container
# restarts without touching the netns — the deadlock this script exists to break, but never a
# leak. The one thing that can refuse it is the `owner` match being unavailable in the pod's
# iptables.
#
# Usage: netns-reset.sh (no arguments)
# Runs as `/opt/netns-reset/netns-reset.sh && exec /init` in the wireguard sidecar container,
# mounted from the qbittorrent-netns-reset ConfigMap. Environment: LAN_NETWORKS (required, the
# comma-separated LAN CIDRs the gate allows) and PUID (required, the uid the gate fences).
# Exit 0 means the netns is reset and the image's init can start; exit 1 means the gate could
# not be installed and nothing was touched.
set -u

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

log() { echo "[netns-reset] $*"; }

lan="${LAN_NETWORKS:?LAN_NETWORKS is unset - the mod needs it for its allow-list and the gate below needs it too}"
uid="${PUID:?PUID is unset - the gate scopes to the uid qbittorrent runs as}"

rules=""
for net in $(echo "$lan" | tr ',' ' '); do
    rules="${rules}-A OUTPUT -d ${net} -j ACCEPT
"
done

if ! iptables-restore <<EOF
*raw
COMMIT
*mangle
COMMIT
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
${rules}-A OUTPUT ! -o wg0 -m mark ! --mark 51820 -m owner --uid-owner ${uid} -m addrtype ! --dst-type LOCAL -j REJECT
COMMIT
EOF
then
    log "FATAL: could not install the uid-${uid} egress gate; leaving the netns untouched"
    exit 1
fi

if ip link show wg0 >/dev/null 2>&1; then
    log "stale wg0 from a previous container in this pod: removing it and its routing state"
    ip link del wg0
fi

# Delete by preference number: wg-quick picks the table number at `up` time.
ip -4 rule show | sed -n 's/^\([0-9][0-9]*\):.*\(fwmark\|suppress_prefixlength\).*/\1/p' \
    | while read -r pref; do ip -4 rule del pref "$pref"; done

# PreDown removes these routes but never runs when a container is killed.
for net in $(echo "$lan" | tr ',' ' '); do
    ip route del "$net" 2>/dev/null || true
done

# Stash table is below 51820: wg-quick counts up from 51820 past any non-empty table.
# Restores the default route LSIO deletes when a tunnel fails; runs after the gate opens eth0.
stash=51819
route=$(ip -4 route show default)
if [ -n "$route" ]; then
    # shellcheck disable=SC2086 # the route's words are ip's arguments
    ip -4 route replace $route table "$stash"
else
    saved=$(ip -4 route show table "$stash" default)
    if [ -n "$saved" ]; then
        log "no default route, as a failed tunnel start leaves the netns: restoring ${saved}"
        # shellcheck disable=SC2086
        ip -4 route add $saved
    else
        log "WARNING: no default route and none stashed; only a pod delete restores it"
    fi
fi

# resolv.conf is a bind mount that outlives a restart: restore it with `cat >`, never cp or mv.
resolv_saved=/config/.netns-reset-resolv.conf
if grep -q '^# Generated by resolvconf' /etc/resolv.conf 2>/dev/null; then
    if [ -s "$resolv_saved" ]; then
        log "resolv.conf still names the previous container's tunnel resolver: restoring the kubelet's"
        cat "$resolv_saved" > /etc/resolv.conf
    else
        log "WARNING: resolv.conf names a tunnel resolver and no kubelet copy is saved; only a pod delete restores it"
    fi
elif ! cp /etc/resolv.conf "$resolv_saved" 2>/dev/null; then
    log "WARNING: could not save the kubelet's resolv.conf to ${resolv_saved}"
fi

log "netns clean; uid-${uid} egress gated to wg0 and ${lan}"
