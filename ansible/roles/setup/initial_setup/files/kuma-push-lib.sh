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
#
# Retries up to twice more (three attempts total), after a fixed backoff, on anything that isn't
# Kuma answering with a genuine permanent rejection — a bad or revoked token, HTTP 401/403
# (retrying cannot fix those, and delaying the log line that surfaces them costs more than it
# buys). Everything else retries: curl itself failing before a response exists (connection
# refused/reset, timeout, TLS — a `curl_rc != 0`), a 5xx, AND a 4xx other than 401/403 —
# in particular 404. That last one is not hypothetical and is, empirically, the DOMINANT case:
# uptime-kuma's Deployment is `strategy: Recreate` on two RWO Longhorn PVCs (a rolling update
# deadlocks on a single-replica volume), so a restart has a real window with zero ready
# endpoints — but Traefik's KubernetesCRD provider drops a router entirely once its service has
# no endpoints, rather than keeping the route and answering through it with a 503. Measured from
# Traefik's access log over the 3 days to 2026-09-03: non-200 responses to `/api/push/` were
# 88 x 404 (empty RouterName, OriginStatus 0 — no router matched and nothing was contacted)
# against 5 x 503, 5 x 500 and 2 x 403. This library shipped classifying a rollout as a 503 and
# stopping on every 4xx including that 404, which made the retry inert against the failure mode
# it was written for (issue #1010) — it passed every test here while firing on none of the real
# rollout pushes, the "green and inert" failure this repo has paid for more than once (repo-root
# CLAUDE.md). So this classifies on the HTTP status curl reports via `-w`, not on `-f`'s collapse
# of it: retry a `curl_rc != 0` or any HTTP code except 2xx/401/403, stop only on 401 or 403. A
# no-router 404 and a bad-token 404 are indistinguishable to curl — the trade is that a
# genuinely bad token now burns the retry budget before its log line appears, which the sibling
# cron `ansible/roles/k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2` already
# accepts and records for the same reason.
#
# Issue #994 measured 49 dropped pushes across 11 crons clustered at three uptime-kuma rollouts,
# with every one of them computed as `status=up` and thrown away — the static monitors run
# `max_retries: 0`, so a dropped push leaves the tile STALE rather than red until the interval
# elapses, and the loss is invisible.
#
# The backoff is a fixed in-library constant, not a caller argument or an env read: the header
# above already commits every caller-identity field to an explicit argument, and a retry-tuning
# knob is a library-internal concern, not identity. Three attempts rather than two: issue #1010
# measured the longest observed endpoint-less window at 31s (12:29:43-12:30:14 on 2026-09-03),
# longer than a single 30s backoff, so a lone retry can still land inside the outage. Three
# attempts at a fixed 30s backoff put the second retry at t=60s, ~2x that window — the same
# margin the crowdsec sibling sized its own retry to. At a 10s --max-time each and two 30s
# backoffs between them, the worst case is 90s — well under the tightest affected cron period
# (the CrowdSec home-allowlist updater, */5 = 300s) — so a genuinely-down Kuma still cannot make
# a cron overlap its next run.
#
# Considered and rejected: raising `max_retries` on the monitors instead. That turns a Kuma
# rollout into a red tile — noise for a cause that is not a real outage — where a delivered
# retry keeps the tile meaning what it says.
kuma_push() {
  local status="$1" msg="$2" push_url="$3" kuma_host="$4" resolve_ip="$5" tag="$6"
  local -r retry_delay_s=30
  local attempt http_code curl_rc
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
  # nothing. No `-f`: it collapses every HTTP >=400 to one exit code, which is exactly the
  # distinction the retry needs to make. `-w '%{http_code}'` reports the status directly instead,
  # and `-o /dev/null` discards the body Kuma's push endpoint returns. The `curl_rc=$? ||`
  # guard on the assignment (rather than reading $? on the next line) keeps this safe under a
  # future `set -e` caller — every current caller runs `set -uo pipefail`, not `-e`, but the
  # function has no way to know what a caller five years from now will set.
  for attempt in 1 2 3; do
    http_code=$(
      printf 'url = "%s"\n' "$push_url" |
        curl -sS --max-time 10 -G -K - \
          --resolve "${kuma_host}:443:${resolve_ip}" \
          --data-urlencode "status=${status}" \
          --data-urlencode "msg=${msg}" \
          -o /dev/null -w '%{http_code}'
    ) && curl_rc=0 || curl_rc=$?
    if [ "$curl_rc" -eq 0 ]; then
      case "$http_code" in
      2??) return 0 ;;
      # 401/403 are Kuma answering with a genuine permanent rejection (bad/revoked token) —
      # retrying cannot fix it. Everything else — curl itself failing with curl_rc != 0, a 5xx,
      # or any OTHER 4xx (in particular a 404: Traefik drops a route entirely once its service
      # has no endpoints, rather than serving a 503 through it — issue #1010) — is the transient
      # case this retry exists for.
      401 | 403) break ;;
      esac
    fi
    if [ "$attempt" -lt 3 ]; then
      logger -t "$tag" "push failed transiently (http=${http_code} rc=${curl_rc}) (status=${status}: ${msg}), retrying in ${retry_delay_s}s"
      sleep "$retry_delay_s"
    fi
  done
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  KUMA_PUSH_OK=0
  # http_code and curl_rc carry the LAST attempt's outcome. They are logged because a rollout's
  # failure mode was measured wrong once already (issue #1010: assumed 503, observed 404 in 88 of
  # 100 non-200 responses) — logging the code lets the next rollout's journal settle the class
  # directly instead of by inference. `journalctl --since ... | grep "push failed"` then reads
  # the actual class. Neither value can carry the push token; both are numbers.
  logger -t "$tag" "push failed (http=${http_code} rc=${curl_rc}) (status=${status}: ${msg})"
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
