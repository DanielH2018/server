#!/usr/bin/env python3
"""Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml.

Three subcommands:
  sync   — reconcile the registry (ansible/secret_rotation.yml) with the live secret
           names. New secrets are classified into a tier and given a STAGGERED seed
           date so their rotations never all fall due on the same day. Removed secrets
           are reported. Existing entries (tier overrides + real rotation dates) are
           preserved.
  audit  — compute which secrets are due / overdue per tier and print a report. With
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
  pinned   730d  MUST NOT be naively swapped (kopia repo password, authelia storage
                 encryption key) — needs a dedicated migration command or backups/DB break
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(REPO, "ansible", "vars", "secrets.yml")
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
_PINNED = {"kopia_password", "authelia_storage", "zigbee_network_key"}
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


def consumer_tag(name: str) -> str | None:
    """Deploy tag whose redeploy makes a rotated push token take effect — or None when the
    consumer spans hosts / is self-referential (those stay MANUAL: the unattended cron skips
    them, the audit still reminds). A push token lives in two places on one compose file: the
    pusher's env AND the AutoKuma `push_token` label, so one redeploy updates both atomically."""
    if name.startswith("monitor_bridge_") or name == "kopia_restore_drill_push_token":
        return "monitor-bridge"
    if name.startswith("cloudflare_ddns_"):
        return "cloudflare-ddns"
    if name == "arr_autoblock_push_token":
        # autofix-bridge (daniel-server only) consumes it in env + the AutoKuma label on one
        # compose file — the single-host single-redeploy pattern, not cross-host. (Token name
        # kept as arr_autoblock_* through the arr-autoblock -> autofix-bridge rename for Kuma
        # history continuity; the consumer is the autofix-bridge deploy tag.)
        return "autofix-bridge"
    return (
        None  # pi_sd_health (Pi cron + server label), secret_rotation (self) -> manual
    )


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
# Secret rotation registry — MANAGED by scripts/secret_rotation.py.
# Plaintext on purpose (names + dates + tiers only, never values); lives outside vars/ so
# SOPS does not encrypt it. Run `secret_rotation.py sync` after adding/removing a secret.
# You MAY edit a `tier` to override classification (sync preserves it); don't hand-edit
# `last_rotated` — `rotate` updates it. Tiers: auto|assisted|external|pinned|ignore.
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


def cmd_audit(args) -> int:
    reg = load_registry()
    # Registry drift: warn by default (so a forgotten `sync` is visible); --check fails on it.
    missing, stale = registry_drift(set(reg.get("secrets", {})), set(secret_names()))
    res = audit(reg, dt.date.today())
    n_over = len(res["overdue"])
    for name, tier, d, days_left in res["all"]:
        flag = "OVERDUE" if days_left < 0 else ("soon" if days_left <= 14 else "ok")
        print("  %-7s %-40s %-9s due %s (%+d d)" % (flag, name, tier, d, days_left))
    parts = ["%d %s" % (c, t) for t, c in sorted(res["by_tier"].items())]
    summary = (
        ("%d secret(s) overdue (%s)" % (n_over, ", ".join(parts)))
        if n_over
        else "all secrets within rotation window"
    )
    if missing:
        summary += "; %d unregistered (run sync)" % len(missing)
    if stale:
        summary += "; %d stale registry entr%s (run sync)" % (
            len(stale),
            "y" if len(stale) == 1 else "ies",
        )
    print("audit:", summary)
    if args.push:
        url = os.environ.get("SECRET_ROTATION_KUMA")
        if not url:
            print("--push set but SECRET_ROTATION_KUMA env missing", file=sys.stderr)
            return 2
        # `stale` too (a registry row for a since-removed secret), so the daily Kuma push and
        # the CI `--check` gate below agree on registry drift — otherwise a `stale`-only drift
        # fails CI while the monitor stays green.
        _push(url, ok=(n_over == 0 and not missing and not stale), msg=summary)
    # --check: a CI/PR gate that the registry is in sync with secrets.yml. Fails ONLY on drift,
    # NOT on overdue (a time-based runtime state the daily Kuma push owns — blocking an unrelated
    # commit on a due-for-rotation secret would be wrong). Read-only (no decrypt), CI-safe.
    if getattr(args, "check", False) and (missing or stale):
        print(
            "secret_rotation: registry out of sync with secrets.yml — run "
            "`uv run python scripts/secret_rotation.py sync` and commit.",
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
        targets = [r for r in due_auto if consumer_tag(r[0])]
        for name, _t, _d, _dl in due_auto:
            if not consumer_tag(name):
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
                % (name, consumer_tag(name) or "?", days_left)
            )
            continue
        new = pysecrets.token_hex(
            16
        )  # 32 hex chars — the format Kuma push tokens require
        subprocess.run(
            ["sops", "set", SECRETS_FILE, '["%s"]' % name, '"%s"' % new],
            check=True,
            cwd=REPO,
        )
        reg["secrets"][name]["last_rotated"] = today.isoformat()
        if consumer_tag(name):
            tags.add(consumer_tag(name))
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
        "--check",
        action="store_true",
        help="exit non-zero if the registry is out of sync with secrets.yml (CI gate)",
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
