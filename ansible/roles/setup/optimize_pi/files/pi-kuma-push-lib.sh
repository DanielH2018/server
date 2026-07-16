#!/usr/bin/env bash
# Shared Kuma-push helper for the optimize_pi health crons (pi-sd-health / pi-recovery-health) —
# managed by Ansible (optimize_pi role); edits overwritten. Sourced from
# /usr/local/lib/pi-kuma-push-lib.sh; not executed directly. Both crons push a status heartbeat to a
# static Kuma push monitor over the LAN-only ^/api/push/ Authelia bypass, resolving Traefik's .local
# router straight to the server LAN IP (no DNS dependency). All fields are passed as args (the caller
# Jinja-renders KUMA_HOST/RESOLVE_IP — the same server host/IP for both crons — and its own token/tag).

# pi_kuma_push STATUS MSG PUSH_URL KUMA_HOST RESOLVE_IP TAG
pi_kuma_push() {
  local status="$1" msg="$2" push_url="$3" kuma_host="$4" resolve_ip="$5" tag="$6"
  curl -fsS --max-time 10 -G \
    --resolve "${kuma_host}:443:${resolve_ip}" \
    --data-urlencode "status=${status}" \
    --data-urlencode "msg=${msg}" \
    "$push_url" >/dev/null \
    || logger -t "$tag" "push failed (status=${status}: ${msg})"
}
