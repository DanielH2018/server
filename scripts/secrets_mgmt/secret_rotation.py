#!/usr/bin/env python3
"""Secret rotation registry: audit + staggered due dates for ansible/vars/secrets.yml.

Four subcommands:
  consumers — list every role that references one secret, measured from the tree, with the
           exact commands that make a rotation take effect. Answers the question a rotation
           poses and `audit` does not: who is still holding the old value? Setup-plane roles
           and host packages are called out separately, each with its own apply command.
  sync   — reconcile the registry (ansible/secret_rotation.yml) with the live secret
           names. New secrets are classified into a tier and given a STAGGERED seed
           date so their rotations never all fall due on the same day. Removed secrets
           are reported. Existing entries (tier overrides + real rotation dates) are
           preserved.
  audit  — compute which secrets are due / overdue per tier and print a report. Dates
           come from the registry, advanced to the date git shows the secret's ciphertext
           last changed — an app-side rotation that `sync` cannot date is otherwise
           invisible and ages into a false OVERDUE. Nothing is written: the registry is
           adjusted in memory only, so git stays the source of truth. A row whose tier
           has no cadence (an unknown or retired tier) reads down: nothing would ever audit
           it. With --verdict, exit 1 when the audit reads down, which the daily audit
           wrapper turns into its textfile verdict; --check fails on drift and unknown tiers.
  record — set one row's `last_rotated` to a date only the operator knows, and save. The
           carry-over after a SOPS rename (`git` cannot date a renamed key past its rename)
           and the date of an app-side rotation nothing in the tree performed. `sync` never
           touches an existing row's date and `audit` advances dates in memory only, so
           without this the registry could be moved only by hand.

This file is the CLI. The logic each subcommand runs on lives in modules that import nothing
from here. `secrets_lib/` holds the two libraries only this tool and its generated page use:
`secret_classify` (tier by name) and `secret_registry` (seeding, sync, due dates, drift).
Beside this file are `consumers` (who holds a copy), `git_dates` (when a ciphertext last
changed) and `rotation_tools` (every process boundary, injectable).

Secret NAMES are read straight from the encrypted secrets.yml — SOPS encrypts values but
leaves keys in plaintext — so `sync` and `audit` run without an age key and are safe in CI.
No subcommand writes a secret's value: a rotation is the runbook in docs/secret-rotation.md.

Tiers (and default rotation cadence):
  assisted 365d  app-issued / coupled (app password, API key, OIDC secret) — needs an
                 app-side step; the audit reminds, rotation is a documented runbook
  external 365d  provider-managed (Cloudflare/Discord/Mullvad/SMTP/LLM keys) — mint in
                 the provider console; audit-only
  pinned   730d  MUST NOT be naively swapped (authelia storage encryption key) — needs a
                 dedicated migration command or backups/DB break
  ignore   —     not a rotatable secret (domain, usernames, static interface addresses)
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser, error
from lib.exit_codes import SECRET_ROTATION_BAD_ARGS
from secrets_mgmt.consumers import consumer_commands, tree_consumers
from secrets_mgmt.git_dates import (
    RENAMED_FROM,
    advance_last_rotated,
    derived_rotation_dates,
)
from secrets_mgmt.secrets_lib.secret_registry import (
    audit,
    registry_drift,
    sync,
    unknown_tiers,
)
from secrets_mgmt.rotation_tools import RotationTools

# A LITERAL, and it must stay one: `scripts/docs/gen_doc_fragments.py` reads this assignment
# with `ast.literal_eval` (it never imports this module, which would need an age key on the
# path for nothing) to build the published tier table. `rotation_tools.DEFAULT_TIER_DAYS`
# holds the same table for `secret_registry.py`, which may not import this file.
TIER_DAYS = {
    "assisted": 365,
    "external": 365,
    "external-90d": 90,
    "pinned": 730,
    "ignore": None,
}


def cmd_consumers(args) -> int:
    """Print every role that references `args.name`, and the commands to redeploy them.

    The one subcommand that crosses no boundary `RotationTools` owns: the tree read behind it
    is parameterised by `repo=` instead. `main` adapts the two-argument dispatch for it.
    """
    consumers = tree_consumers(args.name)
    if not consumers:
        print(
            "%s: no role references this secret.\n"
            "That is either a genuinely unused secret or a name that is built up in a "
            "template rather than written literally — check by hand before concluding it is "
            "safe to drop." % args.name
        )
        return 0
    print("%s — %d consuming role(s):" % (args.name, len(consumers)))
    for role in sorted(consumers):
        plane = consumers[role]
        note = {
            "deploy": "",
            "host": "   [host package — operator-only host_secrets.py sync]",
        }.get(plane, "   [setup plane — its playbook applies it]")
        print("  %-22s %s%s" % (role, plane, note))
    print("\nTo make a rotation take effect:")
    for command in consumer_commands(args.name):
        print("  %s" % command)
    return 0


def cmd_sync(args, tools: RotationTools) -> int:
    """Reconcile the registry with secrets.yml, save it, and print what changed."""
    reg = tools.load_registry()
    added, stale = sync(reg, tools.sops_names(), tools.today(), tools.tier_days)
    tools.save_registry(reg)
    print("sync: %d added, %d stale" % (len(added), len(stale)))
    for n in added:
        print("  + %-40s %s" % (n, reg["entries"][n]["tier"]))
    for n in stale:
        print("  ! stale (in registry, not in secrets.yml): %s" % n)
    return 0


def audit_summary(
    res: dict, missing: list, stale: list, unknown: list | None = None
) -> str:
    """The one-line status the audit prints last, which the daily wrapper's journal keeps.

    NAMES the overdue secrets (most-overdue first, capped), so the operator can tell which
    credential came due without SSHing in to read the full report. `unknown` is the rows whose
    tier has no cadence, which `audit` cannot date at all.
    """
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
    if unknown:
        summary += "; %d row(s) with no tier cadence: %s" % (
            len(unknown),
            ", ".join(unknown),
        )
    return summary


def cmd_audit(args, tools: RotationTools) -> int:
    """Print each secret's rotation status and its summary line, and gate on drift.

    Exits 1 when `--check` is given and the registry is out of sync with secrets.yml or a row
    carries a tier `tier_days` has no entry for (`unknown_tiers`), and 1
    when `--verdict` is given and the audit reads down — the daily audit runs under a systemd
    timer whose `Restart=on-failure` reruns it every 30 min until it exits 0
    (homelab-secret-rotation-audit.service), and its wrapper writes the exit code as
    the textfile verdict SecretRotationAuditFailed reads. Without `--verdict`, overdue secrets
    are reported but never fail this exit code; those are the daily audit's concern, not a CI
    gate's.
    """
    reg = tools.load_registry()
    # Registry drift: warn by default (so a forgotten `sync` is visible); --check fails on it.
    missing, stale = registry_drift(
        set(reg.get("entries", {})), set(tools.sops_names())
    )
    # A real rotation changes the ciphertext in git but leaves `last_rotated` behind,
    # because `sync` deliberately won't touch an existing value's date. Reading the date
    # back out of git closes that gap without writing the registry.
    advanced = (
        []
        if args.no_derive
        else advance_last_rotated(reg, derived_rotation_dates(tools))
    )
    for name, old, new in advanced:
        print("  rotated in git, date advanced: %-30s %s -> %s" % (name, old, new))
    res = audit(reg, tools.today(), tools.tier_days)
    n_over = len(res["overdue"])
    for name, tier, d, days_left in res["all"]:
        flag = "OVERDUE" if days_left < 0 else ("soon" if days_left <= 14 else "ok")
        print("  %-7s %-40s %-9s due %s (%+d d)" % (flag, name, tier, d, days_left))
    unknown = unknown_tiers(reg, tools.tier_days)
    for name in unknown:
        print(
            "  UNKNOWN %-40s %-9s no cadence: nothing audits this tier"
            % (name, reg["entries"][name].get("tier"))
        )
    summary = audit_summary(res, missing, stale, unknown)
    # An externally-detected fault the caller wants folded in, such as a revoked `gh` token.
    # ADDITIVE, deliberately: the fault says nothing about the secrets, so forcing DOWN while
    # still enumerating the audit keeps overdue reporting alive until a human clears it.
    extra_down = getattr(args, "extra_down", None)
    if extra_down:
        summary = f"{extra_down}; {summary}"
        print("audit: forced DOWN by --extra-down")
    # `stale` too (a registry row for a since-removed secret), so the daily verdict and the CI
    # `--check` gate below agree on registry drift — otherwise a `stale`-only drift fails CI
    # while the daily verdict stays up.
    ok = n_over == 0 and not missing and not stale and not unknown and not extra_down
    print("audit: verdict=%s %s" % ("up" if ok else "down", summary))
    if getattr(args, "verdict", False) and not ok:
        return 1
    # --check: a CI/PR gate that the registry is in sync with secrets.yml and every tier has a
    # cadence. NOT on overdue (a time-based runtime state the daily verdict owns — blocking an
    # unrelated commit on a due-for-rotation secret would be wrong). Read-only (no decrypt).
    if getattr(args, "check", False) and (missing or stale):
        error(
            "registry out of sync with secrets.yml — run "
            "`uv run python scripts/secrets_mgmt/secret_rotation.py sync` and commit."
        )
        return 1
    if getattr(args, "check", False) and unknown:
        error(
            "registry rows %s carry a tier with no cadence; the tiers are %s"
            % (", ".join(unknown), ", ".join(tools.tier_days))
        )
        return 1
    return 0


def cmd_record(args, tools: RotationTools) -> int:
    """Set `args.key`'s `last_rotated` to `args.last_rotated` and save the registry.

    Moves the date in EITHER direction, unlike `advance_last_rotated`: that overlays git
    evidence on a seed and may only clear an overdue, where here the operator is the
    evidence, and the rename carry-over moves the date BACKWARD on purpose — git dates a
    renamed key to its rename commit at best, and `sync` would seed it fresher still.

    A key the registry lacks is refused (exit 2) unless it is a `RENAMED_FROM` target whose
    source row exists: then the row moves to the new name, keeping its tier and `source`,
    so a rename is three commands and no YAML edit. A date after today is refused (exit 2).
    Not the `source: record` field — that marks a value SOPS only records; this records a
    DATE, for any row.
    """
    reg = tools.load_registry()
    entries = reg.setdefault("entries", {})
    if args.last_rotated > tools.today():
        error("%s is after today (%s)" % (args.last_rotated, tools.today()))
        return SECRET_ROTATION_BAD_ARGS
    moved_from = None
    was = RENAMED_FROM.get(args.key)
    if was is not None and was in entries:
        # A `sync` run before this would have seeded the new name too; the source row is
        # the one carrying the operator's tier and `source`, so it wins.
        entries[args.key] = entries.pop(was)
        moved_from = was
    if args.key not in entries:
        error(
            "%s is not in the registry — run `secret_rotation.py sync` first, or "
            "add it to RENAMED_FROM (scripts/secrets_mgmt/git_dates.py) if this is a rename"
            % args.key
        )
        return SECRET_ROTATION_BAD_ARGS
    old = entries[args.key].get("last_rotated")
    entries[args.key]["last_rotated"] = args.last_rotated.isoformat()
    tools.save_registry(reg)
    print(
        "record: %s last_rotated %s -> %s%s"
        % (
            args.key,
            old,
            args.last_rotated.isoformat(),
            " (row moved from %s)" % moved_from if moved_from else "",
        )
    )
    return 0


def build_parser() -> ArgumentParser:
    """The command line: one subparser per subcommand, each naming its function as `func`."""
    p = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(func=cmd_sync)
    pc = sub.add_parser("consumers")
    pc.add_argument("name", help="secret name, as it appears in secrets.yml")
    # `consumers` crosses no boundary, so it takes no `tools`. The adapter keeps the dispatch
    # below uniform rather than making every other subcommand carry an argument for this one.
    pc.set_defaults(func=lambda args, _tools: cmd_consumers(args))
    pa = sub.add_parser("audit")
    pa.add_argument(
        "--verdict",
        dest="verdict",
        action="store_true",
        help="exit 1 when the audit reads down (the daily wrapper's textfile verdict)",
    )
    pa.add_argument(
        "--extra-down",
        metavar="REASON",
        help="force DOWN and prefix REASON to the summary line, while still reporting "
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
    prec = sub.add_parser(
        "record",
        help="set one row's last_rotated to a date only the operator knows (a rename's "
        "carry-over, an app-side rotation) — unrelated to the `source: record` field",
    )
    prec.add_argument(
        "--key", required=True, help="secret name, as it appears in secrets.yml"
    )
    prec.add_argument(
        "--last-rotated",
        required=True,
        type=dt.date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="the date the credential was last rotated; may be earlier than what is recorded",
    )
    prec.set_defaults(func=cmd_record)
    return p


def main(argv=None) -> int:
    """Dispatch to the `sync`/`consumers`/`audit`/`record` subcommand and return its exit code."""
    args = build_parser().parse_args(argv)
    # One `RotationTools` per run, built here and threaded down: every git call, sops call,
    # registry read or write and clock read a subcommand makes goes through it.
    return args.func(args, RotationTools())


if __name__ == "__main__":
    raise SystemExit(main())
