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
  # The URL embeds the push token, so it goes in via a config file on stdin rather than as an
  # argv element. /proc is mounted without hidepid here, so any local user can read another
  # user's /proc/<pid>/cmdline; disk-health and longhorn-backup-health run every 10 minutes, and
  # health-crons.yml:48 documents a mix of root and unprivileged cron owners. Moving the token
  # out of the world-readable rendered scripts (2026-08-23) left this last argv exposure — a
  # sub-second window per run instead of a persistent file, but not zero.
  #
  # Status and msg stay as --data-urlencode: they carry no secret, and -G appends them to the
  # config-supplied URL exactly as before. No caller reads stdin, so `-K -` conflicts with
  # nothing.
  printf 'url = "%s"\n' "$push_url" |
    curl -fsS --max-time 10 -G -K - \
      --resolve "${kuma_host}:443:${resolve_ip}" \
      --data-urlencode "status=${status}" \
      --data-urlencode "msg=${msg}" \
      >/dev/null \
    || { # shellcheck disable=SC2034  # read by the sourcing script, not by this file
      KUMA_PUSH_OK=0
      logger -t "$tag" "push failed (status=${status}: ${msg})"
    }
  return 0
}

# boot_grace_active GRACE_S TAG
#
# True (exit 0) when this host booted less than GRACE_S ago, meaning the caller should skip this
# run. A frequent health cron fires within seconds of boot: the 2026-08-30 restart brought
# daniel-box up at 07:39:48 and the */10 crons ran at 07:40:00, twelve seconds later, while k3s,
# Longhorn and Uptime-Kuma were still starting — the last pod reached Ready at 07:45:06. Both
# healthchecks.io dead-men those crons feed send `/fail` on a failed run, and a `/fail` alerts
# IMMEDIATELY regardless of the check's configured grace, so period/grace tuning cannot reach this
# case at all. Skipping the run instead lets the dead-man's own period+grace cover the gap.
#
# GRACE_S MUST be shorter than the caller's cron interval, so at most ONE slot is ever skipped and
# the dead-man's grace only has to tolerate a single miss. See k3s_health_cron_boot_grace_s in the
# k3s role's defaults for the derivation, and docs/healthchecks-io-deadman.md for the grace table
# it produces.
#
# Fails OPEN: an unreadable /proc/uptime returns non-zero so the caller runs its check anyway. A
# guard that cannot read the clock must not silence monitoring indefinitely.
boot_grace_active() {
  local grace="$1" tag="$2" up
  up=$(cut -d. -f1 </proc/uptime 2>/dev/null) || return 1
  [ -n "$up" ] || return 1
  [ "$up" -lt "$grace" ] || return 1
  logger -t "$tag" "boot grace: uptime ${up}s < ${grace}s — skipped, dead-man grace covers it"
  return 0
}
