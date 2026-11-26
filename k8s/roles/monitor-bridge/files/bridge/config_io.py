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
import json

# Defaults live here, not in checks/wan.py, because `bridge/` is a leaf every check imports. Two
# independent providers, by hostname.
WAN_PROBE_DEFAULT = (
    "https://cloudflare.com/cdn-cgi/trace,https://www.google.com/generate_204"
)


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
    B2_LOCK_PROBE_INTERVAL_S: float
    R2_S3_KEY_ID: str = field(repr=False)
    R2_S3_SECRET_KEY: str = field(repr=False)
    R2_LOCK_PROBE_INTERVAL_S: float
    R2_ETCD_LOCK_DAYS: int
    CLOUDFLARE_IPS_EXPECTED: frozenset[str]
    CLOUDFLARE_IPS_PROBE_INTERVAL_S: float
    DOMAIN_RDAP_URL: str
    DOMAIN_REGISTRATION_PROBE_INTERVAL_S: float
    WAN_PROBE_URLS: tuple[str, ...]
    HEALTHCHECKS_API_URL: str
    HEALTHCHECKS_API_KEY: str = field(repr=False)
    HEALTHCHECKS_EXPECTED: tuple[dict, ...]
    HEALTHCHECKS_PROBE_INTERVAL_S: float
    UPTIME_ROBOT_API_URL: str
    UPTIME_ROBOT_API_KEY: str = field(repr=False)
    UPTIME_ROBOT_MONITORS: tuple[dict, ...]
    UPTIME_ROBOT_PROBE_INTERVAL_S: float
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
    _int: Callable[..., int],
    _num: Callable[..., float],
    _env_file: Callable[..., str],
    problems: list[str],
) -> IoConfig:
    """The storage, log and alerting fields, read through `load_config`'s parsers."""

    def _expected_checks(raw: str) -> tuple[dict, ...]:
        """HEALTHCHECKS_EXPECTED's JSON list. Malformed is a config problem, never a raise."""
        if not raw.strip():
            return ()
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(
                isinstance(c, dict)
                and c.get("slug")
                and c.get("kind") in ("simple", "cron")
                for c in parsed
            ):
                raise ValueError("not a list of {slug, kind: simple|cron, ...} objects")
        except ValueError as e:
            problems.append(
                "HEALTHCHECKS_EXPECTED is malformed (%s); the console drift check is off"
                % e
            )
            return ()
        return tuple(parsed)

    def _uptime_robot_monitors(raw: str) -> tuple[dict, ...]:
        """UPTIME_ROBOT_MONITORS's JSON list. Malformed is a config problem, never a raise."""
        if not raw.strip():
            return ()
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(
                isinstance(m, dict)
                and isinstance(m.get("id"), str)
                and isinstance(m.get("name"), str)
                for m in parsed
            ):
                raise ValueError("not a list of {id, name} objects with string values")
        except ValueError as e:
            problems.append(
                "UPTIME_ROBOT_MONITORS is malformed (%s); the Uptime Robot check is off"
                % e
            )
            return ()
        return tuple(parsed)

    return IoConfig(
        # B2 caps TRANSACTIONS separately from bytes, so a check reporting the last cron run reads
        # green while B2 refuses every request.
        # The probe makes one Class C call (b2_authorize_account) then one Class B call
        # (checks/b2.py:b2_authorize); a cap breach answers either with 403
        # `transaction_cap_exceeded`.
        # ASSUMPTION, untestable without a live breach: each call is subject to its class's cap.
        # Class A is free and uncapped, so it has no probe.
        B2_PROBE_URL=_env(
            "B2_PROBE_URL", "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
        ),
        # Read through _env_file: `envFrom` has no per-key filter. Named B2_PROBE_*, not
        # LONGHORN_B2_*, so a scoped read-only key can replace it as an inventory edit.
        B2_PROBE_KEY_ID=_env_file("B2_PROBE_KEY_ID"),
        B2_PROBE_APPLICATION_KEY=_env_file("B2_PROBE_APPLICATION_KEY"),
        # DECIDED: both outcomes cache for B2_PROBE_INTERVAL_S, and only a genuinely successful
        # auth clears a DOWN; it held DOWN through all four real cap incidents.
        # Probe at most this often, and cache BOTH outcomes until it expires. Every other gate
        # re-probes each cycle; this one must not, because the failure it detects is a
        # transaction cap and an uncached probe would add 288 calls/day (INTERVAL=300) to the
        # very budget it is watching. Caching only successes — the EMAIL_PROBE_INTERVAL_S idiom
        # — would be worse than useless here: it retries on failure, so a cap breach would drive
        # the full 288 rejected calls/day into an exhausted cap. At 1800s this is 48 calls/day
        # flat, and detection lands within 30 min of a breach that last time went 9.5 hours
        # unactioned.
        B2_PROBE_INTERVAL_S=_num("B2_PROBE_INTERVAL_S"),
        # The TTL for a failure that never reached B2 is one INTERVAL, not B2_PROBE_INTERVAL_S: a
        # connection that never landed costs no transaction, so the next cycle re-probes.
        B2_TRANSPORT_RETRY_S=_num("B2_TRANSPORT_RETRY_S"),
        # Listing versions is the only way to see the real number: hidden and unfinished versions
        # bill as stored bytes but do not appear in a plain object listing.
        B2_STORAGE_CAP_BYTES=_num("B2_STORAGE_CAP_BYTES", str(10 * 1000**3)),
        B2_STORAGE_MAX_PCT=_num("B2_STORAGE_MAX_PCT"),
        # Daily, because a full listing costs one Class C call per 1000 versions and a budget guard
        # must not be a meaningful part of the spend.
        B2_STORAGE_INTERVAL_S=_num("B2_STORAGE_INTERVAL_S"),
        # Stop paging at this bound; hitting it is reported, because a truncated sum under-reports
        # usage.
        B2_STORAGE_MAX_PAGES=_int("B2_STORAGE_MAX_PAGES", "50"),
        # A headroom guard, not a bill-shock alarm: Cloudflare offers no spending cap on R2, so this
        # notices a runaway client while the fix is a config edit.
        # CF_ANALYTICS_TOKEN is file-mounted; its only permission is Account Analytics: Read, so the
        # check pages and never revokes.
        CF_GRAPHQL_URL=_env(
            "CF_GRAPHQL_URL", "https://api.cloudflare.com/client/v4/graphql"
        ),
        CF_ACCOUNT_ID=_env("CF_ACCOUNT_ID"),
        CF_ANALYTICS_TOKEN=_env_file("CF_ANALYTICS_TOKEN"),
        R2_BUCKET=_env("R2_BUCKET"),
        R2_STORAGE_MAX_GB=_num("R2_STORAGE_MAX_GB"),
        R2_CLASS_A_MAX=_num("R2_CLASS_A_MAX"),
        R2_CLASS_B_MAX=_num("R2_CLASS_B_MAX"),
        R2_USAGE_MAX_PCT=_num("R2_USAGE_MAX_PCT"),
        # Incomplete multipart uploads bill as stored bytes but do not appear in a normal listing;
        # the durable fix is a bucket lifecycle rule (see this role's CLAUDE.md), and this arm
        # notices its absence.
        R2_UPLOADS_MAX=_num("R2_UPLOADS_MAX"),
        # SUCCESSES are cached, a failure re-probes next cycle: opposite of B2's cache-both, because
        # GraphQL analytics calls are free and count against no R2 budget.
        # The action class lists are not env vars; they live in verdicts/storage.py beside
        # r2_classify_operations.
        R2_PROBE_INTERVAL_S=_num("R2_PROBE_INTERVAL_S", "1800"),
        # A lock setting changes only by hand, and each read costs two Class C calls.
        B2_LOCK_PROBE_INTERVAL_S=_num("B2_LOCK_PROBE_INTERVAL_S"),
        R2_S3_KEY_ID=_env_file("R2_S3_KEY_ID"),
        R2_S3_SECRET_KEY=_env_file("R2_S3_SECRET_KEY"),
        # How long check_r2_etcd_lock holds a verdict, either outcome: one canary a day.
        R2_LOCK_PROBE_INTERVAL_S=_num("R2_LOCK_PROBE_INTERVAL_S"),
        # The Age of ADR-0041's R2 lock rule over etcd-snapshots/, in days.
        R2_ETCD_LOCK_DAYS=_int("R2_ETCD_LOCK_DAYS"),
        # Empty disables the drift check; the interval caches SUCCESSES only, so a red verdict
        # re-probes every cycle.
        CLOUDFLARE_IPS_EXPECTED=frozenset(
            c.strip() for c in _env("CLOUDFLARE_IPS_EXPECTED").split(",") if c.strip()
        ),
        CLOUDFLARE_IPS_PROBE_INTERVAL_S=_num("CLOUDFLARE_IPS_PROBE_INTERVAL_S"),
        # Empty disables the check; successes cached a day, failures re-probed every cycle.
        DOMAIN_RDAP_URL=_env("DOMAIN_RDAP_URL"),
        DOMAIN_REGISTRATION_PROBE_INTERVAL_S=_num(
            "DOMAIN_REGISTRATION_PROBE_INTERVAL_S"
        ),
        # URLs, not hosts, so resolution, connect and TLS are all probed by one request; empty
        # disables the gate.
        WAN_PROBE_URLS=tuple(
            u.strip()
            for u in _env("WAN_PROBE_URLS", WAN_PROBE_DEFAULT).split(",")
            if u.strip()
        ),
        # The key is the project's READ-ONLY API key, file-mounted; either empty disables the check.
        # Successes cached a day, failures re-probed.
        HEALTHCHECKS_API_URL=_env(
            "HEALTHCHECKS_API_URL", "https://healthchecks.io/api/v3/checks/"
        ),
        HEALTHCHECKS_API_KEY=_env_file("HEALTHCHECKS_API_KEY"),
        HEALTHCHECKS_EXPECTED=_expected_checks(_env("HEALTHCHECKS_EXPECTED")),
        HEALTHCHECKS_PROBE_INTERVAL_S=_num("HEALTHCHECKS_PROBE_INTERVAL_S"),
        # The key is the account's READ-ONLY API key, file-mounted; either empty disables the check.
        # Successes cached a day, failures re-probed.
        UPTIME_ROBOT_API_URL=_env(
            "UPTIME_ROBOT_API_URL", "https://api.uptimerobot.com/v2/getMonitors"
        ),
        UPTIME_ROBOT_API_KEY=_env_file("UPTIME_ROBOT_API_KEY"),
        UPTIME_ROBOT_MONITORS=_uptime_robot_monitors(_env("UPTIME_ROBOT_MONITORS")),
        UPTIME_ROBOT_PROBE_INTERVAL_S=_num("UPTIME_ROBOT_PROBE_INTERVAL_S"),
        # GET-verifies the webhook still exists (200 with metadata, 404 once gone), never a POST, so
        # no test message reaches the channel.
        # Empty URL disables the check; the streak hysteresis rides out a transient blip.
        DISCORD_WEBHOOK_URL=_env("DISCORD_WEBHOOK_URL"),
        # CrowdSec POSTs to this webhook directly, not via Alertmanager, so a revoked one silently
        # drops ban notifications. Empty = not checked.
        DISCORD_CROWDSEC_WEBHOOK_URL=_env("DISCORD_CROWDSEC_WEBHOOK_URL"),
        # The GitOps/Renovate webhook is a THIRD independent hop: it delivers both the
        # gitops-deploy rollback alert AND every renovate_notify manual-action digest, neither
        # via Alertmanager, so a rotated/revoked webhook here drops every digest silently.
        # Verify it too. Empty = not checked.
        DISCORD_GITOPS_WEBHOOK_URL=_env("DISCORD_GITOPS_WEBHOOK_URL"),
        # Sonarr/Radarr/Prowlarr POST onHealthIssue alerts directly to this webhook, so a revoked
        # one drops them while every container-up check stays green. Empty = not checked.
        DISCORD_ARR_WEBHOOK_URL=_env("DISCORD_ARR_WEBHOOK_URL"),
        # The healthchecks.io app's own Discord webhook is a FIFTH independent hop: healthchecks
        # POSTs its own check-down/up alerts to it via a "webhook" notification channel (config
        # lives only in hc.sqlite, not templated), NOT via Alertmanager. A rotated/revoked URL
        # silently drops those. It's a redundant secondary path (healthchecks' primary alert route is SMTP
        # email, and it self-logs send failures in hc.sqlite), but it's still an independent
        # delivery hop worth verifying. Empty = skipped.
        DISCORD_HEALTHCHECKS_WEBHOOK_URL=_env("DISCORD_HEALTHCHECKS_WEBHOOK_URL"),
        DISCORD_CONSECUTIVE=_int("DISCORD_CONSECUTIVE"),
        # SMTP login probe for the Alertmanager email backstop, folded into check_discord so a
        # revoked app-password flips it down.
        # Throttled to EMAIL_PROBE_INTERVAL_S because Gmail flags frequent AUTHs: a success is
        # cached, a failure re-probes. Empty SMTP_PASSWORD disables it.
        SMTP_HOST=_env("SMTP_HOST", "smtp.gmail.com"),
        SMTP_PORT=_int("SMTP_PORT", "465"),
        SMTP_USER=_env("SMTP_USER"),
        SMTP_PASSWORD=_env("SMTP_PASSWORD"),
        EMAIL_PROBE_INTERVAL_S=_num("EMAIL_PROBE_INTERVAL_S", "21600"),  # 6h
    )
