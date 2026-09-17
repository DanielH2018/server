#!/bin/sh
# Reset the pod's network namespace before the wireguard sidecar's own init runs.
#
# Runs as the sidecar container's entrypoint wrapper, on EVERY start of that container. The
# first start of a pod finds nothing to remove. A restart within a running pod finds the
# previous container's tunnel state, because the netns belongs to the pod and outlives the
# container: the wg0 link, wg-quick's policy-routing rules, the LAN routes the Mullvad mod's
# PostUp added, and the kill-switch REJECT in the OUTPUT chain. Left in place, that state is
# self-sustaining twice over — the mod's api.mullvad.net call routes into the dead wg0 and
# fails, and even when it succeeds, PostUp's `ip route add` hits the routes that already exist
# and wg-quick tears the tunnel back down (see this role's CLAUDE.md, "A VPN kill-switch
# outlives the container it fenced" — 9h/107 restarts on 2026-08-16, 3.5d/1005 restarts on
# 2026-09-13, both cleared only by a pod delete). This script is what makes a container
# restart equivalent to that pod delete.
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
# `! -o wg0` is true. Without the mark exemption the gate rejected every carrier packet for
# uid 1000 on 2026-09-17: the tunnel was up, root reached the internet through it, and
# qbittorrent saw only timeouts (DHT 0 nodes, every peer and tracker dead). wg-quick sets
# the mark (`wg set wg0 fwmark 51820`) and the mod's own REJECT exempts it the same way.
#
# Fails closed. If the gate cannot be installed, the script exits non-zero and the container
# restarts without touching the netns — the deadlock this script exists to break, but never a
# leak. The one thing that can refuse it is the `owner` match being unavailable in the pod's
# iptables, which the first deploy of this wrapper proves one way or the other.
set -u

log() { echo "[netns-reset] $*"; }

lan="${LAN_NETWORKS:?LAN_NETWORKS is unset - the mod needs it for its allow-list and the gate below needs it too}"
uid="${PUID:?PUID is unset - the gate scopes to the uid qbittorrent runs as}"

rules=""
for net in $(echo "$lan" | tr ',' ' '); do
    rules="${rules}-A OUTPUT -d ${net} -j ACCEPT
"
done

# One atomic restore: the OUTPUT gate replaces whatever the previous container left in filter,
# and the raw/mangle tables wg-quick populates (its CONNMARK and anti-spoof rules) are cleared
# in the same call. A pod netns holds nothing else in any of these tables.
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

# wg-quick's two policy rules (`not fwmark N lookup N`, `lookup main suppress_prefixlength 0`).
# Deleted by preference number: the table number is chosen at `up` time and not knowable here.
ip -4 rule show | sed -n 's/^\([0-9][0-9]*\):.*\(fwmark\|suppress_prefixlength\).*/\1/p' \
    | while read -r pref; do ip -4 rule del pref "$pref"; done

# The LAN routes PostUp added via the pod's default gateway. PreDown, which removes them, never
# runs when a container is killed.
for net in $(echo "$lan" | tr ',' ' '); do
    ip route del "$net" 2>/dev/null || true
done

log "netns clean; uid-${uid} egress gated to wg0 and ${lan}"
