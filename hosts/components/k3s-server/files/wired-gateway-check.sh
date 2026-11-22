#!/usr/bin/env bash
# Report whether the LAN gateway answers over the node's wired link, as a node_exporter textfile
# verdict.
#
# Usage: wired-gateway-check.sh
# homelab-wired-gateway.timer runs it every minute on a node whose host_vars set
# k3s_node_static_link. The unit sets WIRED_LINK to that link and WIRED_GATEWAY to lan_router_ip.
# It writes check `wired-gateway` with success 1 when one of three pings bound to WIRED_LINK gets
# a reply, and 0 otherwise. A DOWN verdict exits 0; the script exits non-zero only when its
# environment is unset or the textfile write fails. Running it by hand is safe and does what the
# timer does.
#
# WHY. The wired default route outranks the WiFi one while the link keeps carrier, so a NIC whose
# transmit queue hangs, or a gateway that stops answering ARP on that port, blackholes the node
# with carrier and operstate still up, where WiredLinkDown reads nothing. The WiredGateway* rules
# in k8s/roles/observability/files/rules/host-checks.yaml read the file this writes.
set -uo pipefail

case "${1:-}" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: wired-gateway-check.sh" >&2; exit 64 ;;
esac

# The packaged unit sets HOMELAB_LIB_DIR to /usr/lib/homelab, where check-lib ships it.
# shellcheck source=/dev/null
source "${HOMELAB_LIB_DIR:-/usr/lib/homelab}/homelab-check-lib.sh" || {
  logger -t wired-gateway "homelab-check-lib.sh missing — install the host package"
  exit 1
}

if [[ -z "${WIRED_LINK:-}" || -z "${WIRED_GATEWAY:-}" ]]; then
  echo "WIRED_LINK and WIRED_GATEWAY must both be set" >&2
  exit 64
fi

# The longest legitimate gap between two textfile writes: three 1-minute timer runs. Move it
# with homelab-wired-gateway.timer. hosts/tests/test_wired_gateway_check.py pins the rules'
# literals to it.
DEADLINE_S=180

if PINGED=$(ping -n -q -c 3 -i 0.5 -W 2 -I "$WIRED_LINK" "$WIRED_GATEWAY" 2>&1); then
  SUCCESS=1
else
  SUCCESS=0
  echo "no reply from ${WIRED_GATEWAY} over ${WIRED_LINK}:"
  echo "$PINGED"
  echo "neighbour entry: $(ip neigh show "$WIRED_GATEWAY" dev "$WIRED_LINK" 2>&1)"
fi

write_check_textfile wired-gateway "$SUCCESS" "$(date +%s)" "$DEADLINE_S" || {
  logger -t wired-gateway "textfile write failed"
  exit 1
}
