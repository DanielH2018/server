"""The outbound half of monitor-bridge's configuration: object storage, logs and alerting.

Backblaze B2, Cloudflare R2, the Loki ingestion and log-pattern arms, the log-shipper drop
counters, the five Discord webhooks and the SMTP backstop.

Two probe intervals here are deliberately opposite in kind and the comments say why:
`B2_PROBE_INTERVAL_S` caches BOTH outcomes because the fault it detects is a spend cap that
retrying makes worse, while `R2_PROBE_INTERVAL_S` and `EMAIL_PROBE_INTERVAL_S` cache only
successes. Composed into `Config` by `bridge/config.py`; imports nothing from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IoConfig:
    """B2, R2, the Loki arms, the shipper drop counters, the webhooks and the SMTP backstop."""

    B2_PROBE_URL: str
    B2_PROBE_KEY_ID: str = field(repr=False)
    B2_PROBE_APPLICATION_KEY: str = field(repr=False)
    B2_PROBE_INTERVAL_S: float
    B2_TRANSPORT_RETRY_S: float
    B2_STORAGE_CAP_BYTES: float
    B2_STORAGE_MAX_PCT: float
    B2_STORAGE_INTERVAL_S: float
    B2_STORAGE_MAX_PAGES: int
    CF_GRAPHQL_URL: str
    CF_ACCOUNT_ID: str
    CF_ANALYTICS_TOKEN: str = field(repr=False)
    R2_BUCKET: str
    R2_STORAGE_MAX_GB: float
    R2_CLASS_A_MAX: float
    R2_CLASS_B_MAX: float
    R2_USAGE_MAX_PCT: float
    R2_UPLOADS_MAX: float
    R2_PROBE_INTERVAL_S: float
    LOKI_STREAM: str
    LOKI_DOCKER_STREAM: str
    LOKI_PI_STREAM: str
    LOKI_WINDOW: str
    LOKI_FILETAIL_WINDOW: str
    LOG_ERROR_SELECTOR: str
    LOG_ERROR_PATTERN: str
    LOG_ERROR_WINDOW: str
    LOG_ERROR_MAX: float
    LOG_ERROR_IGNORE: str
    SHIPPER_DROPPED_METRICS: str
    SHIPPER_DROPPED_SERVER_METRIC: str
    SHIPPER_DROPPED_WINDOW: str
    SHIPPER_DROPPED_MAX: float
    DISCORD_WEBHOOK_URL: str = field(repr=False)
    DISCORD_CROWDSEC_WEBHOOK_URL: str = field(repr=False)
    DISCORD_GITOPS_WEBHOOK_URL: str = field(repr=False)
    DISCORD_ARR_WEBHOOK_URL: str = field(repr=False)
    DISCORD_HEALTHCHECKS_WEBHOOK_URL: str = field(repr=False)
    DISCORD_CONSECUTIVE: int
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USER: str
    SMTP_PASSWORD: str = field(repr=False)
    EMAIL_PROBE_INTERVAL_S: float


def io_config(
    _env: Callable[..., str],
    _int: Callable[[str, str], int],
    _num: Callable[[str, str], float],
    _env_file: Callable[..., str],
    interval: int,
) -> IoConfig:
    """The storage, log and alerting fields, read through `load_config`'s parsers."""
    return IoConfig(
        # B2 REACHABILITY — the gap the 2026-08-02 transaction-cap incident exposed
        # (docs/b2-transaction-cap-monitoring-gaps.md). B2 caps TRANSACTIONS separately from
        # storage bytes; the kopia-era state-file checks this used to gate reported their last
        # successful cron run rather than current B2 health, so all of them read green — "B2
        # 6.05/10GB billable (60% of plan)" among them — for nine and a half hours while B2
        # refused every request. Worse than absent — an operator triaging the one true alert was
        # told by these that B2 was fine. Those checks were removed 2026-08-10 (kopia is
        # retired, backup moved to Longhorn — see
        # docs/archive/k3s-migration/backup-consolidation-longhorn.md), but this probe stays:
        # Longhorn still needs B2 reachable.
        #
        # The probe authenticates against B2's native API. A cap breach answers
        # b2_authorize_account with HTTP 403 and error code `transaction_cap_exceeded`, which
        # _get_json's HTTPError detail carries verbatim into the alert message, naming the cause
        # directly.
        #
        # ASSUMPTION, stated because it is load-bearing and cannot be tested without a live
        # breach: that b2_authorize_account is itself subject to the cap. Backblaze's endpoint
        # documentation lists 403/transaction_cap_exceeded among its errors, which is the basis.
        # If a future breach shows THIS monitor stayed green through it, the assumption was
        # wrong — point B2_PROBE_URL at a Class C call (b2_list_buckets, which needs the account
        # id from the auth response) instead. It is a URL swap, not a rewrite, deliberately.
        B2_PROBE_URL=_env(
            "B2_PROBE_URL", "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
        ),
        # Read through _env_file for the same reason as HA_TOKEN: envFrom has no per-key filter,
        # so a secret in monitor-bridge-env is a secret in every process's environment
        # (2026-07-15 review H2). Named B2_PROBE_* rather than KOPIA_B2_* even though the k8s
        # Secret feeds it the `kopia_b2_*` values — those are LONGHORN's B2 credentials, not
        # Kopia's (ADR-0014; Kopia retired 2026-08-13, the key name outlived it because renaming
        # means a rotation). This probe only needs to authenticate, so swapping in a scoped
        # read-only key later is an inventory edit, not a code one.
        B2_PROBE_KEY_ID=_env_file("B2_PROBE_KEY_ID"),
        B2_PROBE_APPLICATION_KEY=_env_file("B2_PROBE_APPLICATION_KEY"),
        # Probe at most this often, and cache BOTH outcomes until it expires. Every other gate
        # re-probes each cycle; this one must not, because the failure it detects is a
        # transaction cap and an uncached probe would add 288 calls/day (INTERVAL=300) to the
        # very budget it is watching. Caching only successes — the EMAIL_PROBE_INTERVAL_S idiom
        # — would be worse than useless here: it retries on failure, so a cap breach would drive
        # the full 288 rejected calls/day into an exhausted cap. At 1800s this is 48 calls/day
        # flat, and detection lands within 30 min of a breach that last time went 9.5 hours
        # unactioned.
        B2_PROBE_INTERVAL_S=_num("B2_PROBE_INTERVAL_S", "1800"),
        # The TTL for a failure that never reached B2 (DNS, connect, timeout). Deliberately NOT
        # B2_PROBE_INTERVAL_S: the whole reason that interval is long is that a probe costs a
        # transaction, and a connection that never landed costs nothing, so the argument above
        # does not apply to it. One INTERVAL, so the next cycle re-probes and the recovery is
        # not held back — see b2_reachable.
        B2_TRANSPORT_RETRY_S=_num("B2_TRANSPORT_RETRY_S", str(interval)),
        # B2 free-tier STORAGE headroom — the other half of the B2 budget, billed separately
        # from the transaction cap b2_reachable watches. kopia reported this as
        # `kopia_b2_billable_bytes`; that metric retired with kopia on 2026-08-10 and nothing
        # replaced it, so the storage half went unwatched at exactly the point Longhorn became
        # its only client. Two Grafana panels kept querying the dead gauge and rendered blank
        # while looking authoritative.
        #
        # Listing versions is the only way to see the real number: hidden and unfinished
        # versions bill as stored bytes and do NOT appear in a plain object listing, which is
        # how the cap filled unnoticed before (hidden kopia bytes wedged retention deletes,
        # 2026-08-13).
        B2_STORAGE_CAP_BYTES=_num("B2_STORAGE_CAP_BYTES", str(10 * 1000**3)),
        B2_STORAGE_MAX_PCT=_num("B2_STORAGE_MAX_PCT", "80"),
        # Daily rather than B2_PROBE_INTERVAL_S: a full listing costs one Class C call per 1000
        # versions, and a check that guards a budget must not be a meaningful part of the spend.
        # At ~5k versions that is ~5 calls/day against a 2500/day Class C allowance.
        B2_STORAGE_INTERVAL_S=_num("B2_STORAGE_INTERVAL_S", "86400"),
        # Stop paging rather than walk forever if the bucket is far larger than expected.
        # Hitting this is itself reported, because a truncated sum under-reports usage — the
        # direction that reads as headroom we do not have.
        B2_STORAGE_MAX_PAGES=_int("B2_STORAGE_MAX_PAGES", "50"),
        # Cloudflare R2 free-tier headroom, via the GraphQL Analytics API. Cloudflare offers NO
        # spending cap or usage limit on R2 — on any plan — so the only boundary is one we watch
        # and act on. The Usage-Based Billing notification that would do this natively needs a
        # Pro plan; this account is on Free. Overage is cheap ($0.015/GB-month, $4.50/M Class A,
        # $0.36/M Class B, egress free), so this is a headroom guard, not a bill-shock alarm: it
        # exists to notice a runaway client (the shape of the 2026-08-13 Longhorn retry storm,
        # which burned ~2.5k B2 Class B/day against a cap) while the fix is still a config edit.
        #
        # CF_ANALYTICS_TOKEN is file-mounted like HA_TOKEN and the B2 key, for the same reason
        # (H2): an account-scoped credential must not sit inline in the container Env. Its ONLY
        # permission is Account Analytics: Read — it cannot touch bucket data, which is why the
        # check reads usage and pages rather than revoking anything itself.
        CF_GRAPHQL_URL=_env(
            "CF_GRAPHQL_URL", "https://api.cloudflare.com/client/v4/graphql"
        ),
        CF_ACCOUNT_ID=_env("CF_ACCOUNT_ID", ""),
        CF_ANALYTICS_TOKEN=_env_file("CF_ANALYTICS_TOKEN"),
        R2_BUCKET=_env("R2_BUCKET", ""),
        R2_STORAGE_MAX_GB=_num("R2_STORAGE_MAX_GB", "10"),
        R2_CLASS_A_MAX=_num("R2_CLASS_A_MAX", "1000000"),
        R2_CLASS_B_MAX=_num("R2_CLASS_B_MAX", "10000000"),
        R2_USAGE_MAX_PCT=_num("R2_USAGE_MAX_PCT", "80"),
        # Outstanding incomplete multipart uploads. These bill as stored bytes but do NOT appear
        # in a normal object listing, so they are the quiet way a 10 GB budget fills. The
        # durable fix is a bucket lifecycle rule (AbortIncompleteMultipartUpload) — a one-time
        # operator step, see this role's CLAUDE.md — and this arm is the backstop that notices
        # when it is absent or not working.
        R2_UPLOADS_MAX=_num("R2_UPLOADS_MAX", "25"),
        # SUCCESSES are cached for this long; a failure re-probes next cycle. The opposite of
        # B2_PROBE_INTERVAL_S's cache-both, and deliberately so: the fault B2 detects is a spend
        # cap that retrying makes worse, whereas GraphQL analytics calls are free and count
        # against no R2 budget. So this follows EMAIL_PROBE_INTERVAL_S's cache-successes-only
        # idiom, and rides out a transient Cloudflare blip through STARTUP_GRACE rather than
        # through a stale cached failure.
        #
        # The Class A / Class B / free ACTION LISTS are not here. They read no env var —
        # Cloudflare's pricing page decides them, not this deployment — so they live in
        # verdicts/storage.py beside r2_classify_operations, their only reader. Moved 2026-09-04.
        R2_PROBE_INTERVAL_S=_num("R2_PROBE_INTERVAL_S", "1800"),
        # Loki log-ingestion freshness: Loki's Kuma /ready probe stays green even when promtail
        # stops SHIPPING (DOCKER_HOST/docker-proxy break, positions-file corruption, relabel
        # regression) — a silently-dead log pipeline that quietly blinds the log dashboards and
        # any future log forensics. Two arms, down if EITHER is silent:
        #   arm 1 (file-tail union): count the file-tailed streams (authlog+syslog+traefik) over
        #   a TOLERANT window (LOKI_FILETAIL_WINDOW) and go down at zero — a promtail
        #   static_configs regression, a stale /var/log bind, or host rsyslog dying silences all
        #   three at once (exactly what /ready can't see), while syslog's routine volume keeps
        #   the union alive on a quiet night so no single low-volume file going quiet trips it.
        #   The selector EXCLUDES the docker_sd stream (promtail stamps it `job: docker`, so a
        #   bare `{job=~".+"}` would swallow it): that stream dwarfs the file-tail streams —
        #   ~all 44 containers' stdout — so including it let a healthy docker stream MASK a total
        #   file-tail outage (arm 1 could then only reach zero if promtail was TOTALLY dead,
        #   which arm 2 already catches — the 2026-07-07 blind-spot review). The window is wider
        #   than arm 2's because file-tail volume is low and dips overnight (a lone
        #   `{job="syslog"}` over 10m false-paged 2026-06-23 — this debloated host routinely
        #   idles >15m between syslog writes).
        #   arm 2 (docker stream): count {container=~".+"} — the docker_sd stream carries a
        #   `container` label, no `job`, so it's exactly the one arm 1 excludes. A
        #   docker_sd-specific break (docker-proxy down, the docker relabel block regressing)
        #   silences every container log while the file-tail streams keep flowing; a tight window
        #   catches a total promtail death fast. Reached at loki:3100 over `monitoring`.
        #   arm 3 (the Pi): both arms above are CLUSTER streams. daniel-pi runs its own promtail,
        #   stamping `job="pi"` — the label LOG_ERROR_SELECTOR already knows about. Nothing
        #   counted it, so the Pi's promtail could die with every cluster stream still flowing
        #   and both arms green: the Pi's logs simply stop arriving and no monitor says so
        #   (2026-08-25 review M-11). The window is the tolerant one, and for a stronger reason
        #   than arm 1's: the Pi is a Zero 2 W running five LAN-only containers, so its log
        #   volume is genuinely low and bursty. `machine!="daniel-pi"` is the SAME masking rule
        #   as the docker_sd exclusion above, applied to a second source that has since started
        #   writing into `job="syslog"`. daniel-pi's promtail now ships its two health crons'
        #   verdict lines under that job (roles/containers/promtail, the pi-health scrape job) so
        #   `probe.py alerts` can reconstruct a Pi episode. Those ~576 lines/day arrive from a
        #   HOST OUTSIDE the cluster, so a total cluster file-tail outage would no longer reach
        #   zero and arm 1 would never fire — the Pi would be holding the alert open on behalf of
        #   the streams it knows nothing about. Loki's `!=` also matches a stream that has no
        #   `machine` label at all, so the cluster's own authlog/syslog/traefik streams are
        #   unaffected. The Pi's own liveness stays covered by arm 3.
        LOKI_STREAM=_env(
            "LOKI_STREAM", '{job=~"authlog|syslog|traefik", machine!="daniel-pi"}'
        ),
        LOKI_DOCKER_STREAM=_env("LOKI_DOCKER_STREAM", '{container=~".+"}'),
        LOKI_PI_STREAM=_env("LOKI_PI_STREAM", '{job="pi"}'),
        LOKI_WINDOW=_env("LOKI_WINDOW", "30m"),
        LOKI_FILETAIL_WINDOW=_env("LOKI_FILETAIL_WINDOW", "3h"),
        # ── log-pattern arm: a workload that is Ready and still failing ──────────────────
        #
        # Every other check here reads a metric or an API. None of them can see a service that
        # answers its probes while logging stack traces — a readiness probe asks "is the port
        # open", not "is the work succeeding". That is the shape of the Grafana dead-panel
        # incident: a 1/1 pod, clean rollout, and 19 panels rendering nothing for 55 minutes.
        #
        # Both estates in one selector. `job="k8s"` is the cluster Alloy shipper's label and
        # `job="pi"` is daniel-pi's, so this arm covered the Pi from the day that shipped.
        LOG_ERROR_SELECTOR=_env("LOG_ERROR_SELECTOR", '{job=~"k8s|pi"}'),
        # Deliberately narrow. `error` is not here and must not be added: it is the single most
        # common word in ordinary application logs (every 404, every retried connection), and an
        # arm that pages on it is an arm that gets muted. These four mean the process itself
        # gave up.
        LOG_ERROR_PATTERN=_env(
            "LOG_ERROR_PATTERN", "(?i)(panic:|fatal|traceback|out of memory)"
        ),
        LOG_ERROR_WINDOW=_env("LOG_ERROR_WINDOW", "1h"),
        # Per container, not estate-wide: one workload melting down must not be diluted by 50
        # quiet ones, and the offender's name is the whole value of the alert.
        LOG_ERROR_MAX=_num("LOG_ERROR_MAX", "20"),
        # Containers whose normal output trips the pattern. Comma-separated, case-insensitive.
        # Keep this list short and say WHY in the inventory — a growing ignore list is the arm
        # decaying.
        LOG_ERROR_IGNORE=_env("LOG_ERROR_IGNORE", ""),
        # Log-shipper dropped-entries watchdog. Prometheus scrapes both shippers' own metrics —
        # the cluster's Alloy DaemonSet (job=alloy) and daniel-pi's Alloy container
        # (job=alloy-pi), both on 12345 — and both expose loki_write_dropped_entries_total. The
        # name is still matched by __name__ regex so a second shipper with a different counter
        # can join without a code change; a selector naming only one estate's counter reads the
        # other as "0 dropped" forever, the fail-open shape of a selector on a label nothing
        # emits (the Pi ran Promtail with its own counter name until 2026-09-02). Loki Log
        # Ingestion only catches TOTAL silence; this surfaces PARTIAL loss — entries a shipper
        # gave up on. NO reason filter (was reason="ingester_error" only): every reason is a real
        # drop, and Loki's own configured limits reject under DIFFERENT reasons the
        # ingester_error-only selector missed entirely — rate_limited (per_stream_rate_limit /
        # ingestion_rate_mb), stream_limited (max_global_streams_per_user), and line_too_long —
        # so a stream explosion or a chatty container hitting the rate cap dropped logs while
        # this stayed green (2026-07-15 review M2). increase() over a window handles counter
        # resets; alert only ABOVE a threshold so a transient Loki restart's handful of drops
        # doesn't page. Prom-dependent (suppressed under the Prometheus gate). No series (counter
        # never incremented) reads as 0 -> up; a dead shipper scrape is Scrape Targets' page, not
        # this one.
        #
        # Known one-off: a shipper's first start re-tails every current file from offset 0 and
        # Loki rejects the already-ingested history as "too far behind", which the shipper counts
        # under reason="ingester_error". Measured at the 2026-09-02 Alloy cutover: 193,348 on
        # daniel-box, nothing lost. That is a page per shipper cutover, not a threshold problem.
        #
        # A metric-name regex rather than a ready-made `{__name__=~...}` selector, because
        # test_check_loki.py treats every `{...}` string field on Config as a LogQL stream
        # selector and checks its labels against the Loki vocabulary — which `__name__` is not.
        SHIPPER_DROPPED_METRICS=_env(
            "SHIPPER_DROPPED_METRICS",
            "loki_write_dropped_entries_total",
        ),
        # The SERVER side of the same pipe (2026-09-03, #993): the shipper counter above only
        # sees what a shipper itself gave up on. Loki's distributor discards entries the shipper
        # never attributes to itself — 161,573 samples (39.4 MB) discarded server-side in a 24h
        # window where the shipper counted 1,027, all under a DIFFERENT reason (ingester_error vs
        # too_far_behind) — so the client-side counter alone understated real loss by ~150x and
        # would have missed a burst the client never logged at all. Scraped from Loki itself
        # (job=loki-homelab) by the same Prometheus every other PROM_DEPENDENT check reads,
        # broken down `by (reason)` in the query so a fired check can name which reason:
        # `too_far_behind` (entries arriving outside Loki's accept window — a clock/backfill
        # problem) versus rate_limited/stream_limited/line_too_long (throughput/limit problems).
        # Shares SHIPPER_DROPPED_WINDOW/_MAX with the client-side arm — both sides count dropped
        # log lines at comparable magnitude, and shipper_dropped() reports whichever side is
        # larger.
        SHIPPER_DROPPED_SERVER_METRIC=_env(
            "SHIPPER_DROPPED_SERVER_METRIC",
            "loki_discarded_samples_total",
        ),
        SHIPPER_DROPPED_WINDOW=_env("SHIPPER_DROPPED_WINDOW", "1h"),
        SHIPPER_DROPPED_MAX=_num("SHIPPER_DROPPED_MAX", "1000"),
        # Discord delivery: Kuma fires every alert by POSTing to its Discord webhook
        # (monitor_discord_webhook_url). A rotated/revoked/deleted webhook leaves every monitor
        # green-in-UI while Discord goes silent — the one link in the alert chain no other
        # monitor (not even the off-box UptimeRobot host dead-man) verifies. We GET-verify the
        # webhook is still valid: Discord answers a webhook GET with its JSON metadata + HTTP 200
        # while it exists and 404s once it's gone — a GET, not a POST, so this never puts a test
        # message in the channel. Empty URL = disabled (stays up), like N8N_API_KEY. The streak
        # hysteresis (like HA_CONSECUTIVE) rides out a transient blip on the one check that
        # reaches the public internet.
        DISCORD_WEBHOOK_URL=_env("DISCORD_WEBHOOK_URL", ""),
        # The CrowdSec ban-alert webhook is a SECOND, independent Discord delivery hop: CrowdSec
        # POSTs directly to it (not via Kuma), so a rotated/revoked CrowdSec webhook silently
        # drops security-ban notifications with NO Kuma backstop. Verify it alongside the Kuma
        # webhook. Empty = not checked.
        DISCORD_CROWDSEC_WEBHOOK_URL=_env("DISCORD_CROWDSEC_WEBHOOK_URL", ""),
        # The GitOps/Renovate webhook is a THIRD independent hop: it delivers both the
        # gitops-deploy rollback alert AND every renovate_notify manual-action digest, neither
        # via Kuma. renovate_notify pushes its "alive" Kuma beat on every clean run regardless of
        # whether the Discord POST succeeded, so a rotated/revoked webhook here leaves the
        # Renovate Notifier — Alive monitor GREEN while every digest silently drops. Verify it
        # too. Empty = not checked.
        DISCORD_GITOPS_WEBHOOK_URL=_env("DISCORD_GITOPS_WEBHOOK_URL", ""),
        # The *arr health/event webhook is a FOURTH independent hop: Sonarr/Radarr/Prowlarr POST
        # their own onHealthIssue alerts (indexer down, download-client errors, app DB errors —
        # signals the Arr Queue check does NOT cover) directly to it via their in-app Discord
        # "Connect", not via Kuma. A rotated/revoked webhook silently drops those while every
        # container-up monitor stays green. Empty = not checked. (The URL lives only in the *arr
        # app DBs + SOPS — this GET-verify is its one watchdog.)
        DISCORD_ARR_WEBHOOK_URL=_env("DISCORD_ARR_WEBHOOK_URL", ""),
        # The healthchecks.io app's own Discord webhook is a FIFTH independent hop: healthchecks
        # POSTs its own check-down/up alerts to it via a "webhook" notification channel (config
        # lives only in hc.sqlite, not templated), NOT via Kuma. A rotated/revoked URL silently
        # drops those. It's a redundant secondary path (healthchecks' primary alert route is SMTP
        # email, and it self-logs send failures in hc.sqlite), but it's still an un-Kuma'd
        # delivery hop worth verifying. Empty = skipped.
        DISCORD_HEALTHCHECKS_WEBHOOK_URL=_env("DISCORD_HEALTHCHECKS_WEBHOOK_URL", ""),
        DISCORD_CONSECUTIVE=_int("DISCORD_CONSECUTIVE", "2"),
        # Alert-email backstop deliverability (folded into check_discord). The uptime-kuma
        # `email` notification (Gmail SMTP) is the independent 2nd channel attached ONLY to the
        # Discord Delivery monitor — the escape hatch when the Kuma Discord webhook is dead (the
        # alert-delivery SPOF). But it had no liveness check of its own, so a silently revoked
        # Gmail app-password could leave that backstop dead undetected and BOTH channels down at
        # once. We fold a throttled SMTP login probe into check_discord: connect + AUTH to
        # SMTP_HOST:SMTP_PORT with the same creds Kuma uses, so a revoked password / broken SMTP
        # flips the Discord Delivery monitor down (which still pages via the working Discord
        # channel). Throttled to EMAIL_PROBE_INTERVAL_S — Gmail flags frequent AUTHs, so a
        # success is cached and only a failure re-probes every cycle. Empty SMTP_PASSWORD =
        # disabled (stays up), like the empty-webhook skips.
        SMTP_HOST=_env("SMTP_HOST", "smtp.gmail.com"),
        SMTP_PORT=_int("SMTP_PORT", "465"),
        SMTP_USER=_env("SMTP_USER", ""),
        SMTP_PASSWORD=_env("SMTP_PASSWORD", ""),
        EMAIL_PROBE_INTERVAL_S=_num("EMAIL_PROBE_INTERVAL_S", "21600"),  # 6h
    )
