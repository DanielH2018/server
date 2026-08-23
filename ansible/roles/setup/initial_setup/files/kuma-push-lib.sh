#!/usr/bin/env bash
# Shared Kuma-push helper for every host's health crons — managed by Ansible (initial_setup
# role); edits overwritten. Sourced from /usr/local/lib/kuma-push-lib.sh; not executed directly.
#
# A cron pushes a status heartbeat to a static Kuma push monitor over the LAN-only ^/api/push/
# Authelia bypass, resolving Traefik's router name straight to an IP so the push does not depend
# on DNS. Fifteen crons across all three hosts had that same six-line curl inlined.
#
# This lived in the optimize_pi role as pi-kuma-push-lib.sh, sourced by the Pi's two health
# crons. Its own header said promoting it was "a refactor worth doing when monitoring itself
# moves onto this host, not before" — the k3s migration moved Uptime-Kuma and monitor-bridge
# into the cluster on 2026-08-14, so the condition it was waiting on is met.
#
# initial_setup owns it because that role runs on every host and runs before deploy.yml (see
# the bring-up order in ansible/README.md §4). A consumer role installing it itself would race
# with the crons of every other role that also needs it.
#
# Every field is an argument: the caller Jinja-renders its own token, host, resolve IP and
# logger tag, and none of them are read from the environment. An implicit read would make a
# caller that forgot to set one push to the wrong monitor silently.

# kuma_push STATUS MSG PUSH_URL KUMA_HOST RESOLVE_IP TAG
#
# STATUS is Kuma's own vocabulary, `up` or `down`. A push failure is logged, and the function
# still returns 0: several callers end their script on this call, and returning non-zero there
# would turn a failed push into a failed cron run — a second alert for the same event.
#
# KUMA_PUSH_OK is set to 1 or 0 for the callers that need the outcome (longhorn-backup-health
# gates its off-site dead-man's-switch ping on it). Reading it is opt-in; ignoring it leaves
# the pre-existing behaviour of every other caller unchanged.
kuma_push() {
  local status="$1" msg="$2" push_url="$3" kuma_host="$4" resolve_ip="$5" tag="$6"
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  KUMA_PUSH_OK=1
  curl -fsS --max-time 10 -G \
    --resolve "${kuma_host}:443:${resolve_ip}" \
    --data-urlencode "status=${status}" \
    --data-urlencode "msg=${msg}" \
    "$push_url" >/dev/null \
    || { # shellcheck disable=SC2034  # read by the sourcing script, not by this file
      KUMA_PUSH_OK=0
      logger -t "$tag" "push failed (status=${status}: ${msg})"
    }
  return 0
}
