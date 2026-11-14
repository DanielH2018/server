#!/usr/bin/env python3
"""The off-site recovery kit's refresh date: record it, and check its age.

The kit (`docs/break-glass.md`, item 8) is the only copy of the repo and the issue register
outside GitHub, and the operator refreshes it by hand. Nothing else in the tree can date a
refresh, so the operator records each one in `secrets/recovery_kit.yml`:

  record — write the refresh date (today, or --date) to the stamp. Commit the file through a
           PR like any other change.
  check  — print the kit's age and exit 0, or print one reason and exit 1 when the stamp is
           missing, unreadable, dated in the future, or older than MAX_AGE_DAYS. The daily
           secret-rotation audit on daniel-box runs it and writes the verdict to its own
           `recovery-kit` check, which RecoveryKitStale reads.

No test or prek hook runs `check`: the stamp ages with the calendar, so a CI check would turn
every unrelated PR red on the day the kit crossed the limit.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import RECOVERY_KIT_BAD_ARGS, RECOVERY_KIT_OK, RECOVERY_KIT_STALE
from lib.repo_paths import REPO

STAMP = REPO / "secrets" / "recovery_kit.yml"

# A quarter: the kit also carries the repo bundle that break-glass promotion renders from, and
# a bundle that predates `prod` cannot serve that runbook.
MAX_AGE_DAYS = 90

RECORD_COMMAND = "uv run python scripts/secrets_mgmt/recovery_kit.py record"

HEADER = (
    "# The off-site recovery kit's last refresh — MANAGED by scripts/secrets_mgmt/recovery_kit.py.\n"
    "# Run `recovery_kit.py record` after each refresh and commit this file. The daily\n"
    "# secret-rotation audit reads it (docs/longhorn-disaster-recovery.md, The off-site recovery kit).\n"
)


def read_refreshed(stamp: Path) -> dt.date | None:
    """The date the stamp records, or None when it is missing or holds no `refreshed:` date."""
    try:
        data = yaml_fast.safe_load(stamp.read_text())
    except OSError, yaml.YAMLError:
        return None
    value = data.get("refreshed") if isinstance(data, dict) else None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def stale_reason(refreshed: dt.date | None, today: dt.date) -> str | None:
    """Why the kit needs a refresh, or None when it is within MAX_AGE_DAYS."""
    if refreshed is None:
        return (
            "no off-site recovery kit refresh is recorded in secrets/recovery_kit.yml; "
            f"refresh the kit, run `{RECORD_COMMAND}` and commit the file"
        )
    if refreshed > today:
        return (
            f"secrets/recovery_kit.yml records a refresh on {refreshed}, after today "
            f"({today}); record the real date with `{RECORD_COMMAND} --date YYYY-MM-DD`"
        )
    age = (today - refreshed).days
    if age > MAX_AGE_DAYS:
        return (
            f"the off-site recovery kit was last refreshed {refreshed}, {age} days ago, past "
            f"the {MAX_AGE_DAYS}-day limit; refresh it and run `{RECORD_COMMAND}`"
        )
    return None


def cmd_check(stamp: Path, today: dt.date) -> int:
    refreshed = read_refreshed(stamp)
    reason = stale_reason(refreshed, today)
    if reason is not None or refreshed is None:
        print(reason)
        return RECOVERY_KIT_STALE
    print(f"recovery kit refreshed {refreshed}, {(today - refreshed).days} days ago")
    return RECOVERY_KIT_OK


def cmd_record(stamp: Path, refreshed: dt.date, today: dt.date) -> int:
    if refreshed > today:
        error(f"{refreshed} is after today ({today})")
        return RECOVERY_KIT_BAD_ARGS
    stamp.write_text(f"{HEADER}refreshed: '{refreshed.isoformat()}'\n")
    print(f"record: recovery kit refreshed {refreshed}; commit {stamp}")
    return RECOVERY_KIT_OK


def build_parser() -> ArgumentParser:
    p = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    record = sub.add_parser("record", help="record a recovery-kit refresh")
    record.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        help="the refresh date, YYYY-MM-DD (default: today)",
    )
    sub.add_parser(
        "check", help=f"exit 1 when the kit is older than {MAX_AGE_DAYS} days"
    )
    return p


def main(argv=None, stamp: Path = STAMP, today: dt.date | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = today or dt.datetime.now(dt.UTC).date()
    if args.cmd == "record":
        return cmd_record(stamp, args.date or today, today)
    return cmd_check(stamp, today)


if __name__ == "__main__":
    sys.exit(main())
