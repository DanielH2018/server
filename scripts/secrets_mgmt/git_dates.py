"""When each secret's ciphertext last changed, read out of git.

A real rotation changes the value in `ansible/vars/secrets.yml` but leaves the registry's
`last_rotated` behind, because `sync` deliberately will not touch an existing row's date. An
app-side rotation nobody recorded is then invisible and ages into a false OVERDUE. These
functions read the date back out of the git history and advance the registry IN MEMORY only,
so git stays the source of truth and the audit stays read-only.

Nothing here decrypts. Every read goes through `tools.git`, so a test drives the whole
derivation off a synthetic history.
"""

from __future__ import annotations

import datetime as dt
import subprocess

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib import yaml_fast
from secrets_mgmt.rotation_tools import RotationTools

# Repo-relative, for git revspecs — `git show <rev>:<path>` needs the tracked path.
# `rotation_tools.SECRETS_FILE` is the absolute spelling of this same file, for the callers
# that open it rather than ask git about it.
SECRETS_GIT_PATH = "ansible/vars/secrets.yml"


def ciphertext_at(rev: str, tools: RotationTools) -> dict[str, str]:
    """name -> stored ciphertext at `rev`.

    Never decrypts: the `diff=sops` textconv driver rewrites diff output only, so `git show
    <rev>:<path>` streams the raw blob.
    """
    data = yaml_fast.safe_load(tools.git("show", f"{rev}:{SECRETS_GIT_PATH}")) or {}
    return {k: str(v) for k, v in data.items() if k != "sops"}


def ciphertext_rotation_dates(tools: RotationTools) -> dict[str, dt.date]:
    """name -> date of the newest commit that changed that secret's ciphertext.

    Compares the parsed value per key rather than the diff text. A commit that only
    reorders or regroups secrets.yml rewrites lines without changing any value, and a
    line-level reader would call every secret freshly rotated — marking genuinely
    overdue ones green. ca5ae25b rewrote 149 of 156 lines doing exactly that.
    """
    revs = [
        line.split(" ", 1)
        for line in tools.git(
            "log", "--format=%H %ad", "--date=short", "--", SECRETS_GIT_PATH
        ).splitlines()
        if line
    ]
    dates: dict[str, dt.date] = {}
    if not revs:
        return dates
    tracked = set(ciphertext_at(revs[0][0], tools))
    newer: dict[str, str] = {}
    newer_day = ""
    for rev, day in revs:
        current = ciphertext_at(rev, tools)
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


def derived_rotation_dates(tools: RotationTools) -> dict[str, dt.date]:
    """Git-derived dates, or {} when git cannot answer (no checkout, shallow clone, git missing).

    The daily cron degrades to the recorded dates instead of failing — a broken derivation must not
    take the monitor down on its own.
    """
    try:
        return ciphertext_rotation_dates(tools)
    except subprocess.CalledProcessError, OSError, yaml.YAMLError, ValueError:
        return {}


def advance_last_rotated(
    reg: dict, dates: dict[str, dt.date]
) -> list[tuple[str, str, str]]:
    """Move `last_rotated` forward where git shows a later change.

    Returns (name, old, new) for each row advanced. Mutates `reg` in memory only — the caller never
    saves it, which is what keeps the audit read-only and git the source of truth.

    Advance-only, for two reasons. Seed dates are deliberately staggered and backdated
    (`secret_registry.seed_last_rotated`) and most secrets predate this file's git history, so
    taking the derived date unconditionally would collapse them onto the same introduction
    commit and un-stagger every due-date. It also means this can only ever clear an overdue secret that a real
    rotation already fixed, never create one.
    """
    # DECIDED: git evidence beats the seed even though it can overstate freshness for a
    # credential minted before this file's first commit (2026-01-17) — such a secret dates
    # to when it was committed, not when it was created. The seed it replaces is not a
    # better reading: `seed_last_rotated` backdates by a hash of the NAME, so it is
    # fiction for every secret nobody has rotated since registration. That fiction is what
    # aged calendar_1 into a false OVERDUE and took the monitor down on 2026-08-25.
    advanced = []
    for name, entry in reg.get("entries", {}).items():
        derived = dates.get(name)
        recorded = entry.get("last_rotated")
        if derived is None or not recorded:
            continue
        if dt.date.fromisoformat(recorded) >= derived:
            continue
        entry["last_rotated"] = derived.isoformat()
        advanced.append((name, recorded, derived.isoformat()))
    return advanced
