#!/usr/bin/env python3
"""Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml.

Four subcommands:
  consumers — list every role that references one secret, measured from the tree, with the
           exact commands that make a rotation take effect. Answers the question a rotation
           poses and `audit` does not: who is still holding the old value? Setup-plane roles
           are called out separately because `deploy.sh` cannot reach them.
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

This file is the CLI. The logic each subcommand runs on lives beside it, in modules that
import nothing from here: `secret_classify` (tier by name), `secret_registry` (seeding, sync, due dates,
drift), `consumers` (who holds a copy), `git_dates` (when a ciphertext last changed),
`sops_io` (push-token shape) and `rotation_tools` (every process boundary, injectable).

Secret NAMES are read straight from the encrypted secrets.yml — SOPS encrypts values but
leaves keys in plaintext — so `sync`, and every arm of `audit` that `--check` gates on, run
without an age key and are safe in CI.

`audit` has one arm that DOES decrypt: the push-token shape check, which is the only way to
see a token Uptime Kuma will refuse. It degrades to "not checked" where there is no age key,
and it reports through the Kuma push only — never through `--check`, so the CI gate stays
decrypt-free. `rotate --commit` also needs the key (it shells out to `sops set`).

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

import argparse
import os
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from secrets_mgmt.consumers import consumer_commands, consumer_tags, tree_consumers
from secrets_mgmt.git_dates import advance_last_rotated, derived_rotation_dates
from secrets_mgmt.secret_registry import audit, registry_drift, sync
from secrets_mgmt.rotation_tools import RotationTools
from secrets_mgmt.sops_io import malformed_push_tokens

# A LITERAL, and it must stay one: `scripts/docs/gen_doc_fragments.py` reads this assignment
# with `ast.literal_eval` (it never imports this module, which would need an age key on the
# path for nothing) to build the published tier table. `rotation_tools.DEFAULT_TIER_DAYS`
# holds the same table for `secret_registry.py`, which may not import this file.
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
        note = (
            "" if plane == "deploy" else "   [setup plane — deploy.sh cannot reach it]"
        )
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


def audit_summary(res: dict, missing: list, stale: list) -> str:
    """The one-line status pushed to the "Secret Rotation" Kuma monitor.

    NAMES the overdue secrets (most-overdue first, capped) — a bare count read identically whether a
    genuine cron break stranded a rotatable token or one of the consumer-less known-manual auto
    tokens (secret_rotation/pi_sd_health/pi_recovery push tokens, which the weekly cron deliberately
    skips) merely came due, so the operator had to SSH in to tell the two apart (2026-07-15 M1).
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
    return summary


def cmd_audit(args, tools: RotationTools) -> int:
    """Print each secret's rotation status, push the summary to Kuma, and gate on drift.

    Exits 2 when `--push` is given without SECRET_ROTATION_KUMA set, 1 when `--check` is
    given and the registry is out of sync with secrets.yml, 0 otherwise — overdue secrets
    and malformed push tokens are reported but never fail this exit code; those are the
    daily Kuma push's concern, not a CI gate's.
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
    summary = audit_summary(res, missing, stale)
    # Push-token shape. This arm decrypts, so it runs only where an age key exists — the daily
    # cron on daniel-box, and an operator's shell. In CI `decrypted_values` returns None and the
    # arm reports "not checked", which is why it feeds the Kuma push and NOT the --check exit
    # code below: --check is the prek gate over secrets.yml / the registry / this file, and a
    # decrypt-dependent verdict there would pass in CI and fail on a developer's machine, turning
    # every future secrets PR red until an unrelated token was fixed.
    values = tools.sops_decrypt()
    if values is None:
        print("  push-token shape: not checked (cannot decrypt here)")
        malformed = []
    else:
        malformed = malformed_push_tokens(values)
        for name, reason in malformed:
            print("  MALFORMED %-40s %s" % (name, reason))
        if not malformed:
            print("  push-token shape: ok")
    if malformed:
        summary += "; %d push token(s) Kuma will reject: %s" % (
            len(malformed),
            ", ".join(n for n, _ in malformed),
        )
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
        ok = (
            n_over == 0
            and not missing
            and not stale
            and not malformed
            and not extra_down
        )
        tools.kuma_push(url, ok, summary)
    # --check: a CI/PR gate that the registry is in sync with secrets.yml. Fails ONLY on drift,
    # NOT on overdue (a time-based runtime state the daily Kuma push owns — blocking an unrelated
    # commit on a due-for-rotation secret would be wrong), and NOT on push-token shape (see the
    # arm above). Read-only (no decrypt), CI-safe.
    if getattr(args, "check", False) and (missing or stale):
        print(
            "secret_rotation: registry out of sync with secrets.yml — run "
            "`uv run python scripts/secrets_mgmt/secret_rotation.py sync` and commit.",
            file=sys.stderr,
        )
        return 1
    return 0


def unattended_due(rows: list, rotate_all: bool = False) -> list:
    """Auto-tier rows the unattended weekly cron should rotate.

    Those due within ROTATE_LEAD_DAYS, or everything auto-tier when `rotate_all`. Rows are
    audit() tuples (name, tier, due_date, days_left).
    """
    return [
        r for r in rows if r[1] == "auto" and (rotate_all or r[3] < ROTATE_LEAD_DAYS)
    ]


def cmd_rotate(args, tools: RotationTools) -> int:
    """Rotate `args.name`, or every coming-due auto-tier secret, and optionally redeploy.

    Dry-run by default; `--commit` writes new values via `sops set`. Exits 2 when
    `args.name` names a non-auto-tier secret, 3 when a `sops set` fails or times out partway
    through the batch, 1 when `--deploy` is given and the redeploy fails (the new tokens are written but
    their consumers are not), 0 otherwise.
    """
    reg = tools.load_registry()
    now = tools.today()
    res = audit(reg, now, tools.tier_days)
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
    written: list[str] = []
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
        try:
            tools.sops_set(name, new)
        except (
            subprocess.CalledProcessError,
            OSError,
            subprocess.TimeoutExpired,
        ) as exc:
            # Each `sops set` writes secrets.yml on its own, so a failure partway leaves the
            # file holding NEW values for the names already done while their dates were only
            # updated in memory. Save the registry first: the two files then agree about what
            # moved, whichever way the caller resolves it, and secret-rotate.sh reverts both
            # together. Do NOT deploy — the tokens that landed are about to be reverted out of
            # the tree, so deploying them would leave the cluster on values nothing records.
            # A TimeoutExpired arrives here too: `sops_set` bounds its write, and a hang is
            # the same half-state as a crash — earlier names are already in the store.
            # `exc` is safe to print: the argv `sops_set` builds carries the secret's NAME and
            # never its value, which is what
            # `test_rotate_commit_sends_new_token_on_stdin_not_argv` pins. TimeoutExpired
            # renders that same argv, and its `.stdout`/`.stderr` are None here because
            # `sops_set` captures neither.
            if written:
                tools.save_registry(reg)
            print(
                "ROTATION FAILED writing %s (%s). Already written to secrets.yml: %s. "
                "Consumers were NOT deployed — revert the working tree (`git checkout -- "
                "ansible/vars/secrets.yml ansible/secret_rotation.yml`) or finish by hand."
                % (name, exc, ", ".join(written) or "nothing"),
                file=sys.stderr,
            )
            return 3
        reg["entries"][name]["last_rotated"] = now.isoformat()
        written.append(name)
        tags.update(consumer_tags(name))
        print("  rotated %s" % name)
    if not args.commit:
        return 0

    tools.save_registry(reg)
    if args.deploy and tags:
        if tools.deploy(sorted(tags)) != 0:
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
    """Dispatch to the `sync`/`consumers`/`audit`/`rotate` subcommand and return its exit code."""
    p = argparse.ArgumentParser(
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
    # One `RotationTools` per run, built here and threaded down: every git call, sops call,
    # registry read or write, Kuma push and clock read a subcommand makes goes through it.
    return args.func(args, RotationTools())


if __name__ == "__main__":
    raise SystemExit(main())
