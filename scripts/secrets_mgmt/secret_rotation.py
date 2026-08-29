#!/usr/bin/env python3
"""Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml.

Three subcommands:
  sync   — reconcile the registry (ansible/secret_rotation.yml) with the live secret
           names. New secrets are classified into a tier and given a STAGGERED seed
           date so their rotations never all fall due on the same day. Removed secrets
           are reported. Existing entries (tier overrides + real rotation dates) are
           preserved.
  audit  — compute which secrets are due / overdue per tier and print a report. Dates
           come from the registry, advanced to the date git shows the secret's ciphertext
           last changed — an app-side rotation that `sync` cannot date is otherwise
           invisible and ages into a false OVERDUE. Nothing is written: the registry is
           adjusted in memory only, so git stays the source of truth. With
           --push, post up/down to an Uptime Kuma push monitor (the SECRET_ROTATION_KUMA
           env var holds the full push URL incl. token).
  rotate — rotate `auto`-tier secrets coming due (locally-generated push tokens — no
           external coupling). Dry-run by default; --commit writes new values via
           `sops set` and records the new date. The unattended path picks up anything
           due within ROTATE_LEAD_DAYS so a token rotates the weekly-cron run BEFORE
           it goes overdue (see the constant's comment); coming-due-only-by-default
           means rotations stay staggered.

Secret NAMES are read straight from the encrypted secrets.yml — SOPS encrypts values but
leaves keys in plaintext — so `audit`/`sync` never decrypt anything and never see a value.
Only `rotate --commit` needs the age key (it shells out to `sops set`).

Tiers (and default rotation cadence):
  auto     180d  locally-generated, no external coupling — this tool can rotate it
  assisted 365d  app-issued / coupled (app password, API key, OIDC secret) — needs an
                 app-side step; the audit reminds, rotation is a documented runbook
  external 365d  provider-managed (Cloudflare/Discord/Mullvad/SMTP/LLM keys) — mint in
                 the provider console; audit-only
  pinned   730d  MUST NOT be naively swapped (authelia storage encryption key) — needs a
                 dedicated migration command or backups/DB break
  ignore   —     not a rotatable secret (domain, usernames, static interface addresses)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import secrets as pysecrets
import subprocess
import sys
import urllib.parse
import urllib.request

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS_FILE = os.path.join(REPO, "ansible", "vars", "secrets.yml")
# Repo-relative, for git revspecs — `git show <rev>:<path>` needs the tracked path.
SECRETS_GIT_PATH = "ansible/vars/secrets.yml"
REGISTRY_FILE = os.path.join(REPO, "ansible", "secret_rotation.yml")

TIER_DAYS = {
    "auto": 180,
    "assisted": 365,
    "external": 365,
    "pinned": 730,
    "ignore": None,
}

# The unattended rotate cron is WEEKLY (Sunday 09:00, initial_setup). Rotating only
# already-overdue tokens would leave each one overdue up to 6 days first — with the daily
# 08:00 audit paging the "Secret Rotation" Kuma monitor DOWN the whole time for a rotation
# that was always going to happen anyway. Anything due within one cron interval (+1 day
# margin) rotates the run BEFORE its due date instead, so a working cron never lets an
# auto token go overdue — an auto-tier OVERDUE in the audit now genuinely means the
# weekly cron is broken, not that it hasn't come around yet.
ROTATE_LEAD_DAYS = 8

# Classification by name. First matching rule wins; default is `assisted` (the safe,
# reminds-but-doesn't-touch tier). Override per-secret by editing `tier` in the registry —
# `sync` preserves overrides.
_IGNORE = {"domain"}
_IGNORE_SUFFIX = ("_user", "_username")
_PINNED = {"authelia_storage", "zigbee_network_key"}
_EXTERNAL = {
    "cloudflare_dns_token",
    "monitor_discord_webhook_url",
    "crowdsec_discord_webhook_url",
    "gitops_deploy_discord_webhook",
    "coinmarket_api_key",
    "karakeep_gemini_api_key",
    "weather_api_key",
    "crowdsec_mapquest_api_key",
    "mullvad_account",
    "email",
    "healthchecks_smtp_password",
    "wireguard_interface_private_key",
}


def classify(name: str) -> str:
    if name in _IGNORE or name.endswith(_IGNORE_SUFFIX):
        return "ignore"
    if name in _PINNED:
        return "pinned"
    if name in _EXTERNAL:
        return "external"
    if name.endswith("_push_token"):
        return "auto"
    return "assisted"


# Push tokens whose pusher and AutoKuma `push_token` label live on DIFFERENT hosts, or which
# reference this script itself — one redeploy cannot update both halves atomically, so these stay
# MANUAL: consumer_tag returns None, the unattended cron skips them, the audit still reminds.
# The single source the guard test derives its allowlist from; a new cross-host token is added
# HERE, with its host pair, not to the test.
CROSS_HOST_PUSH_TOKENS = frozenset(
    {
        "pi_sd_health_push_token",  # Pi cron + daniel-server label
        "pi_recovery_push_token",  # Pi cron + daniel-server label
        "longhorn_backup_push_token",  # daniel-box cron (k3s role) + daniel-server label
        "claude_otel_push_token",  # daniel-box cron (k8s/claude-otel) + daniel-server label
        "daniel_box_disk_push_token",  # daniel-box cron (k3s role) + daniel-server label
        "manifest_prune_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "live_drift_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "etcd_snapshot_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "remember_logs_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        # daniel-box cron (setup/initial_setup) + k8s/uptime-kuma static tile. Both halves embed
        # the token and must move together; initial_setup has no deploy tag, so a `--deploy
        # --tags uptime-kuma` would rotate the tile and leave the cron pushing the old value —
        # which reads as the monitor going silent, the exact fault it was added to report.
        "docs_refresh_push_token",
        "secret_rotation_push_token",  # self-referential
        # Pushed by a setup role with no deploy tag, so there is nothing for --deploy to run.
        # Named `monitor_bridge_*` only for Kuma monitor-history continuity after the check
        # moved out of monitor-bridge (2026-08-25 review M-8b).
        "monitor_bridge_fake_remux_push_token",  # setup/fake_remux cron
        "monitor_bridge_fake_remux_replace_push_token",  # setup/fake_remux cron
        "monitor_bridge_renovate_alive_push_token",  # setup/renovate_notify
        # Same reason as its two fake_remux siblings above: pushed by a setup role with no deploy
        # tag, so there is nothing for --deploy to run. The tile is in k8s/uptime-kuma.
        "mkv_attachment_repair_push_token",
        # nut_host cron (renders /etc/nut/kuma-push.env) + k8s/uptime-kuma static tile. Same shape
        # as docs_refresh_push_token above: nut_host runs only from initial_setup.yml and has no
        # deploy tag, so `--deploy --tags uptime-kuma` would move the tile and leave the root cron
        # pushing the old value — silencing the monitor that watches the shutdown chain.
        "ups_secondary_push_token",
    }
)


# Push tokens whose name carries the `monitor_bridge_` prefix but whose PUSHER lives in another
# role entirely. The prefix is a Kuma-history artefact: the monitor was created by monitor-bridge
# and renaming it would break its history, so the token kept the name after the check moved out
# into the owning service's own health script. Routing these by prefix names a role that renders
# them NOWHERE — `rotate --deploy` would write a new value, deploy monitor-bridge, leave the real
# pusher on the old token and stamp `last_rotated` green (2026-08-25 review M-8b).
#
# Derived by measurement, not by reading the names: `grep -rl <token> ansible/roles/`. Nine of
# the 41 `monitor_bridge_*` tokens mis-routed; the review reported two.
PREFIX_EXCEPTION_CONSUMERS = {
    "monitor_bridge_appsec_push_token": "crowdsec",
    "monitor_bridge_home_allowlist_push_token": "crowdsec",
    "monitor_bridge_cloudflare_drift_push_token": "traefik",
    "monitor_bridge_configarr_push_token": "configarr",
    "monitor_bridge_janitorr_push_token": "janitorr",
    "monitor_bridge_pi_peers_push_token": "pi-peer-backup",
}
# The other three mis-routed tokens are pushed by SETUP roles (setup/fake_remux,
# setup/renovate_notify), which have no entry in `containers_list` and therefore no deploy tag.
# Naming the role here would be the same defect one step along: `ansible-playbook deploy.yml
# --tags fake_remux` matches nothing and Ansible exits 0, so the rotation would still stamp
# green having deployed nothing. They decline instead, below.


# Every push token has TWO consumers in the cluster, and until 2026-08-28 this function named
# only one of them. The pusher reads it from its own role's env Secret; the Kuma monitor that
# receives the push is a static AutoKuma entity rendered by k8s/uptime-kuma
# (`static-monitors.yaml.j2`, a manifests_secret_file). AutoKuma reconciles the live monitor's
# `push_token` FROM that Secret, so a rotation that redeploys only the pusher leaves Kuma
# expecting the old token: the bridge then pushes a token nothing matches, the monitor stops
# beating, and it goes DOWN. That is loud rather than silent, but it is a self-inflicted outage
# on every rotated push monitor, and `rotate --deploy` stamped `last_rotated` green through it.
#
# Measured 2026-08-28 against the live registry and template: 43 tokens resolve a consumer,
# 42 of them have a tile, and the single exception is `monitor_bridge_ha_token` — an HA API
# token that carries the prefix for Kuma history reasons but is not a push token at all. So
# `_push_token` is the exact discriminator, and `test_uptime_kuma_is_a_consumer_iff_a_tile_
# exists` derives the split from the template rather than trusting this comment.
UPTIME_KUMA_TAG = "uptime-kuma"


def consumer_tags(name: str) -> tuple[str, ...]:
    """Deploy tags whose redeploy makes a rotated push token take effect — EMPTY when the
    consumer spans hosts / is self-referential (those stay MANUAL: the unattended cron skips
    them, the audit still reminds).

    Plural, and a tuple, since 2026-08-28. The pre-migration docstring here said a push token
    "lives in two places on one compose file", which was true under Docker+AutoKuma labels and
    false after the k3s migration split the pusher and the tile into two roles. Both roles
    deploy from daniel-box in ONE playbook run, so both tags are reachable by a single
    `rotate --deploy` — which is exactly what distinguishes this from CROSS_HOST_PUSH_TOKENS,
    where the two halves sit on different HOSTS and no redeploy can cover them. Those still
    return empty; a multi-tag return there would assert a repair that cannot happen.
    """
    # Both of these precede the prefix rule below: every token they name also carries the
    # `monitor_bridge_` prefix, so the prefix rule would otherwise claim them first.
    if name in CROSS_HOST_PUSH_TOKENS:
        return ()
    elif name in PREFIX_EXCEPTION_CONSUMERS:
        pusher: str | None = PREFIX_EXCEPTION_CONSUMERS[name]
    elif name.startswith("monitor_bridge_"):
        pusher = "monitor-bridge"
    elif name.startswith("cloudflare_ddns_"):
        pusher = "cloudflare-ddns"
    elif name == "docker_fleet_push_token":
        # The monitor-bridge role renders the host cron script; the monitor itself is a
        # static-file entity in the cluster Kuma (slice-7 Phase D KD2).
        pusher = "monitor-bridge"
    elif name == "arr_autoblock_push_token":
        # autofix-bridge (daniel-server only) renders the pusher's env. (Token name kept as
        # arr_autoblock_* through the arr-autoblock -> autofix-bridge rename for Kuma history
        # continuity; the consumer is the autofix-bridge deploy tag.)
        pusher = "autofix-bridge"
    else:
        # anything else unrecognised -> manual
        return ()
    if name.endswith("_push_token"):
        return (pusher, UPTIME_KUMA_TAG)
    return (pusher,)


def _stable_offset(name: str, span: int) -> int:
    """Deterministic 0..span-1 from the name — spreads seed dates so due-dates fan out."""
    if span <= 0:
        return 0
    return int(hashlib.sha256(name.encode()).hexdigest(), 16) % span


def seed_last_rotated(name: str, tier: str, today: dt.date) -> str | None:
    """A staggered seed date: due = seed + cadence lands in [today+lead, today+cadence],
    so nothing is overdue at registration and the due-dates are spread across the window."""
    days = TIER_DAYS[tier]
    if not days:
        return None
    lead = max(14, days // 12)
    offset = _stable_offset(name, days - lead)
    return (today - dt.timedelta(days=offset)).isoformat()


def secret_names(path: str = SECRETS_FILE) -> list[str]:
    """Top-level secret keys from the (encrypted) secrets.yml — values stay encrypted."""
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return sorted(k for k in data if k != "sops")


def load_registry(path: str = REGISTRY_FILE) -> dict:
    if not os.path.exists(path):
        return {"secrets": {}}
    with open(path) as fh:
        return yaml.safe_load(fh) or {"secrets": {}}


_HEADER = """\
# Secret rotation registry — MANAGED by scripts/secrets_mgmt/secret_rotation.py.
# Plaintext on purpose (names + dates + tiers only, never values); lives outside vars/ so
# SOPS does not encrypt it. Run `secret_rotation.py sync` after adding/removing a secret.
# You MAY edit a `tier` to override classification (sync preserves it); don't hand-edit
# `last_rotated` — `rotate` updates it, and `audit` reads the real date out of the git
# history of secrets.yml when a value changed later than this file records.
# Tiers: auto|assisted|external|pinned|ignore.
"""


def save_registry(reg: dict, path: str = REGISTRY_FILE) -> None:
    body = yaml.safe_dump(reg, sort_keys=True, default_flow_style=False)
    with open(path, "w") as fh:
        fh.write(_HEADER)
        fh.write(body)


def sync(reg: dict, names: list[str], today: dt.date) -> tuple[list[str], list[str]]:
    """Add missing secrets (classified + staggered seed); report stale registry entries."""
    entries = reg.setdefault("secrets", {})
    added, stale = [], []
    for name in names:
        if name not in entries:
            tier = classify(name)
            entries[name] = {
                "tier": tier,
                "last_rotated": seed_last_rotated(name, tier, today),
            }
            added.append(name)
    live = set(names)
    stale = sorted(n for n in entries if n not in live)
    return added, stale


def due_date(entry: dict) -> dt.date | None:
    tier = entry.get("tier", "assisted")
    days = TIER_DAYS.get(tier)
    lr = entry.get("last_rotated")
    if not days or not lr:
        return None
    return dt.date.fromisoformat(lr) + dt.timedelta(days=days)


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def ciphertext_at(rev: str) -> dict[str, str]:
    """name -> stored ciphertext at `rev`. Never decrypts: the `diff=sops` textconv
    driver rewrites diff output only, so `git show <rev>:<path>` streams the raw blob."""
    data = yaml.safe_load(_git("show", f"{rev}:{SECRETS_GIT_PATH}")) or {}
    return {k: str(v) for k, v in data.items() if k != "sops"}


def ciphertext_rotation_dates() -> dict[str, dt.date]:
    """name -> date of the newest commit that changed that secret's ciphertext.

    Compares the parsed value per key rather than the diff text. A commit that only
    reorders or regroups secrets.yml rewrites lines without changing any value, and a
    line-level reader would call every secret freshly rotated — marking genuinely
    overdue ones green. ca5ae25b rewrote 149 of 156 lines doing exactly that.
    """
    revs = [
        line.split(" ", 1)
        for line in _git(
            "log", "--format=%H %ad", "--date=short", "--", SECRETS_GIT_PATH
        ).splitlines()
        if line
    ]
    dates: dict[str, dt.date] = {}
    if not revs:
        return dates
    tracked = set(ciphertext_at(revs[0][0]))
    newer: dict[str, str] = {}
    newer_day = ""
    for rev, day in revs:
        current = ciphertext_at(rev)
        for name, value in newer.items():
            if name not in dates and current.get(name) != value:
                dates[name] = dt.date.fromisoformat(newer_day)
        if tracked <= set(dates):
            break
        newer, newer_day = current, day
    # Whatever never changed existed unaltered back to the oldest revision, so that
    # revision is the best evidence of when its value was set.
    for name in newer:
        dates.setdefault(name, dt.date.fromisoformat(newer_day))
    return dates


def derived_rotation_dates() -> dict[str, dt.date]:
    """Git-derived dates, or {} when git cannot answer (no checkout, shallow clone, git
    missing). The daily cron degrades to the recorded dates instead of failing — a broken
    derivation must not take the monitor down on its own."""
    try:
        return ciphertext_rotation_dates()
    except subprocess.CalledProcessError, OSError, yaml.YAMLError, ValueError:
        return {}


def advance_last_rotated(
    reg: dict, dates: dict[str, dt.date]
) -> list[tuple[str, str, str]]:
    """Move `last_rotated` forward where git shows a later change. Returns (name, old,
    new) for each row advanced. Mutates `reg` in memory only — the caller never saves it,
    which is what keeps the audit read-only and git the source of truth.

    Advance-only, for two reasons. Seed dates are deliberately staggered and backdated
    (`seed_last_rotated`) and most secrets predate this file's git history, so taking the
    derived date unconditionally would collapse them onto the same introduction commit
    and un-stagger every due-date. It also means this can only ever clear an overdue
    secret that a real rotation already fixed, never create one.
    """
    # DECIDED: git evidence beats the seed even though it can overstate freshness for a
    # credential minted before this file's first commit (2026-01-17) — such a secret dates
    # to when it was committed, not when it was created. The seed it replaces is not a
    # better reading: `seed_last_rotated` backdates by a hash of the NAME, so it is
    # fiction for every secret nobody has rotated since registration. That fiction is what
    # aged calendar_1 into a false OVERDUE and took the monitor down on 2026-08-25.
    advanced = []
    for name, entry in reg.get("secrets", {}).items():
        derived = dates.get(name)
        recorded = entry.get("last_rotated")
        if derived is None or not recorded:
            continue
        if dt.date.fromisoformat(recorded) >= derived:
            continue
        entry["last_rotated"] = derived.isoformat()
        advanced.append((name, recorded, derived.isoformat()))
    return advanced


def audit(reg: dict, today: dt.date) -> dict:
    """Returns {overdue: [...], soon: [...], by_tier: {...}} sorted by urgency."""
    rows = []
    for name, entry in reg.get("secrets", {}).items():
        d = due_date(entry)
        if d is None:
            continue
        rows.append((name, entry.get("tier"), d, (d - today).days))
    rows.sort(key=lambda r: r[3])
    overdue = [r for r in rows if r[3] < 0]
    soon = [r for r in rows if 0 <= r[3] <= 14]
    by_tier: dict[str, int] = {}
    for _, tier, _, days_left in rows:
        if days_left < 0:
            by_tier[tier] = by_tier.get(tier, 0) + 1
    return {"overdue": overdue, "soon": soon, "by_tier": by_tier, "all": rows}


def _push(url: str, ok: bool, msg: str) -> None:
    full = "%s?status=%s&msg=%s" % (
        url,
        "up" if ok else "down",
        urllib.parse.quote(msg),
    )
    urllib.request.urlopen(full, timeout=10).read()


def cmd_sync(args) -> int:
    reg = load_registry()
    added, stale = sync(reg, secret_names(), dt.date.today())
    save_registry(reg)
    print("sync: %d added, %d stale" % (len(added), len(stale)))
    for n in added:
        print("  + %-40s %s" % (n, reg["secrets"][n]["tier"]))
    for n in stale:
        print("  ! stale (in registry, not in secrets.yml): %s" % n)
    return 0


def registry_drift(registered: set, present: set) -> tuple[list, list]:
    """Pure registry-vs-secrets.yml drift. Returns (missing, stale):
      missing = in secrets.yml but NOT in the registry (a `sync` was forgotten after /add-secret);
      stale   = a registry row whose secret was removed from secrets.yml.
    Reads plaintext key NAMES only — never decrypts a value, so it's CI-safe."""
    return sorted(present - registered), sorted(registered - present)


def audit_summary(res: dict, missing: list, stale: list) -> str:
    """The one-line status pushed to the "Secret Rotation" Kuma monitor. NAMES the overdue
    secrets (most-overdue first, capped) — a bare count read identically whether a genuine cron
    break stranded a rotatable token or one of the consumer-less known-manual auto tokens
    (secret_rotation/pi_sd_health/pi_recovery push tokens, which the weekly cron deliberately
    skips) merely came due, so the operator had to SSH in to tell the two apart (2026-07-15 M1)."""
    n_over = len(res["overdue"])
    parts = ["%d %s" % (c, t) for t, c in sorted(res["by_tier"].items())]
    if n_over:
        names = [r[0] for r in res["overdue"]]
        shown = ", ".join(names[:5]) + (
            (" +%d more" % (len(names) - 5)) if len(names) > 5 else ""
        )
        summary = "%d secret(s) overdue (%s): %s" % (n_over, ", ".join(parts), shown)
    else:
        summary = "all secrets within rotation window"
    if missing:
        summary += "; %d unregistered (run sync)" % len(missing)
    if stale:
        summary += "; %d stale registry entr%s (run sync)" % (
            len(stale),
            "y" if len(stale) == 1 else "ies",
        )
    return summary


def cmd_audit(args) -> int:
    reg = load_registry()
    # Registry drift: warn by default (so a forgotten `sync` is visible); --check fails on it.
    missing, stale = registry_drift(set(reg.get("secrets", {})), set(secret_names()))
    # A real rotation changes the ciphertext in git but leaves `last_rotated` behind,
    # because `sync` deliberately won't touch an existing value's date. Reading the date
    # back out of git closes that gap without writing the registry.
    advanced = (
        [] if args.no_derive else advance_last_rotated(reg, derived_rotation_dates())
    )
    for name, old, new in advanced:
        print("  rotated in git, date advanced: %-30s %s -> %s" % (name, old, new))
    res = audit(reg, dt.date.today())
    n_over = len(res["overdue"])
    for name, tier, d, days_left in res["all"]:
        flag = "OVERDUE" if days_left < 0 else ("soon" if days_left <= 14 else "ok")
        print("  %-7s %-40s %-9s due %s (%+d d)" % (flag, name, tier, d, days_left))
    summary = audit_summary(res, missing, stale)
    # An externally-detected fault the caller wants folded in. ADDITIVE, deliberately: the
    # caller could short-circuit to its own sticky DOWN instead (the two arms above it in
    # secret-rotation-audit.sh.j2 do exactly that), but those describe a registry that cannot
    # be trusted, whereas an unlanded rotation branch says nothing about the other secrets.
    # Short-circuiting there would silence overdue reporting for all of them until a human
    # cleared the branch. Forcing DOWN while still enumerating the audit keeps both signals.
    extra_down = getattr(args, "extra_down", None)
    if extra_down:
        summary = f"{extra_down}; {summary}"
        print("audit: forced DOWN by --extra-down")
    if args.push:
        url = os.environ.get("SECRET_ROTATION_KUMA")
        if not url:
            print("--push set but SECRET_ROTATION_KUMA env missing", file=sys.stderr)
            return 2
        # `stale` too (a registry row for a since-removed secret), so the daily Kuma push and
        # the CI `--check` gate below agree on registry drift — otherwise a `stale`-only drift
        # fails CI while the monitor stays green.
        ok = n_over == 0 and not missing and not stale and not extra_down
        _push(url, ok=ok, msg=summary)
    # --check: a CI/PR gate that the registry is in sync with secrets.yml. Fails ONLY on drift,
    # NOT on overdue (a time-based runtime state the daily Kuma push owns — blocking an unrelated
    # commit on a due-for-rotation secret would be wrong). Read-only (no decrypt), CI-safe.
    if getattr(args, "check", False) and (missing or stale):
        print(
            "secret_rotation: registry out of sync with secrets.yml — run "
            "`uv run python scripts/secrets_mgmt/secret_rotation.py sync` and commit.",
            file=sys.stderr,
        )
        return 1
    return 0


def unattended_due(rows: list, rotate_all: bool = False) -> list:
    """Auto-tier rows the unattended weekly cron should rotate: due within
    ROTATE_LEAD_DAYS (everything auto-tier with rotate_all). Rows are audit()
    tuples (name, tier, due_date, days_left)."""
    return [
        r for r in rows if r[1] == "auto" and (rotate_all or r[3] < ROTATE_LEAD_DAYS)
    ]


def cmd_rotate(args) -> int:
    reg = load_registry()
    today = dt.date.today()
    res = audit(reg, today)
    if args.name:
        targets = [r for r in res["all"] if r[0] == args.name]
        if targets and targets[0][1] != "auto":
            print(
                "refusing: %s is tier '%s', not auto-rotatable"
                % (args.name, targets[0][1]),
                file=sys.stderr,
            )
            return 2
    else:
        # Unattended path: auto-tier, coming due (unless --all), AND with a single-redeploy
        # consumer. Tokens with no consumer_tag (cross-host / self-referential) are reported
        # but skipped.
        due_auto = unattended_due(res["all"], args.all)
        targets = [r for r in due_auto if consumer_tags(r[0])]
        for name, _t, _d, _dl in due_auto:
            if not consumer_tags(name):
                print("  skip (manual: cross-host consumer) %s" % name)
    if not targets:
        print(
            "rotate: nothing to rotate in the auto tier"
            + ("" if args.all else " today")
        )
        return 0

    tags = set()
    for name, _tier, _d, days_left in targets:
        if not args.commit:
            print(
                "  DRY-RUN would rotate %-40s -> %s (due %+d d)"
                % (name, ",".join(consumer_tags(name)) or "?", days_left)
            )
            continue
        new = pysecrets.token_hex(
            16
        )  # 32 hex chars — the format Kuma push tokens require
        # --value-stdin keeps the new token out of argv (world-readable via /proc/<pid>/cmdline
        # here — no hidepid). It still requires a JSON-encoded value, same as the old argv
        # form, so the quoting stays; only the transport moves to stdin.
        subprocess.run(
            ["sops", "set", "--value-stdin", SECRETS_FILE, '["%s"]' % name],
            input='"%s"' % new,
            text=True,
            check=True,
            cwd=REPO,
        )
        reg["secrets"][name]["last_rotated"] = today.isoformat()
        tags.update(consumer_tags(name))
        print("  rotated %s" % name)
    if not args.commit:
        return 0

    save_registry(reg)
    if args.deploy and tags:
        cmd = [
            "uv",
            "run",
            # --frozen: never mutate uv.lock (parity with the GitOps deployer) — a lock
            # rewrite here leaves the tree dirty and wedges the next weekly run's
            # clean-tree check in secret-rotate.sh.
            "--frozen",
            "ansible-playbook",
            "ansible/deploy.yml",
            "--tags",
            ",".join(sorted(tags)),
        ]
        print("  deploying:", " ".join(cmd))
        r = subprocess.run(cmd, cwd=REPO)
        if r.returncode != 0:
            print(
                "DEPLOY FAILED — new tokens written to secrets.yml but consumers NOT updated; "
                "the caller should revert the working tree",
                file=sys.stderr,
            )
            return 1
    elif not args.deploy:
        print(
            "\nNext: redeploy the consumer(s): "
            "uv run ansible-playbook ansible/deploy.yml --tags %s"
            % ",".join(sorted(tags))
        )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(func=cmd_sync)
    pa = sub.add_parser("audit")
    pa.add_argument("--push", action="store_true", help="post status to Uptime Kuma")
    pa.add_argument(
        "--extra-down",
        metavar="REASON",
        help="force DOWN and prefix REASON to the pushed message, while still reporting "
        "every overdue secret (for a fault the caller detected, not the registry)",
    )
    pa.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the registry is out of sync with secrets.yml (CI gate)",
    )
    pa.add_argument(
        "--no-derive",
        action="store_true",
        help="trust last_rotated as recorded; skip reading rotation dates out of git",
    )
    pa.set_defaults(func=cmd_audit)
    pr = sub.add_parser("rotate")
    pr.add_argument(
        "--commit", action="store_true", help="actually write (default: dry-run)"
    )
    pr.add_argument("--all", action="store_true", help="all auto secrets, not only due")
    pr.add_argument("--name", help="rotate one named auto secret")
    pr.add_argument(
        "--deploy", action="store_true", help="redeploy consumers after rotating"
    )
    pr.set_defaults(func=cmd_rotate)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
