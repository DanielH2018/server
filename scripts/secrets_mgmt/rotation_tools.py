"""Every process boundary `secret_rotation.py` crosses, as one injectable object.

A test replaces one field of `RotationTools` and never a module attribute. The defaults are
the real implementations, defined HERE rather than beside their callers: `secret_rotation.py`
is the entry point, so a default imported from it would be a cycle — and, when that file runs
as `__main__`, a second copy of the module under a second name. **This module names
`secret_rotation` nowhere**, at import time or later, which is what makes it a leaf every
other module in the package can import. The one fact both files hold is the tier table, and
each spells the literal out; `DEFAULT_TIER_DAYS` below says why, and which test holds the two
equal.
"""

import datetime as dt
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from zoneinfo import ZoneInfo

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib import yaml_fast
from lib.git import git
from secrets_mgmt.sops_io import REPO, SECRETS_FILE

REGISTRY_FILE = os.path.join(REPO, "ansible", "secret_rotation.yml")


def run_git(*args: str) -> str:
    """Run `git <args>` in the repo and return its stdout."""
    return git(*args, cwd=REPO).stdout


def today() -> dt.date:
    """The registry's calendar day.

    Rotation dates are day-granular and the hosts, the crons and the operator all live in
    America/Chicago, so the day is pinned to that zone rather than to whichever TZ the invoking
    shell happens to carry.
    """
    return dt.datetime.now(tz=ZoneInfo("America/Chicago")).date()


def sops_names(path: str = SECRETS_FILE) -> list[str]:
    """Top-level secret keys from the (encrypted) store — values stay encrypted."""
    with open(path) as fh:
        data = yaml_fast.safe_load(fh) or {}
    return sorted(k for k in data if k != "sops")


def load_registry(path: str = REGISTRY_FILE) -> dict:
    """The rotation registry as parsed YAML; an empty skeleton when the file is absent."""
    if not os.path.exists(path):
        return {"entries": {}}
    with open(path) as fh:
        return yaml_fast.safe_load(fh) or {"entries": {}}


_HEADER = """\
# Secret rotation registry — MANAGED by scripts/secrets_mgmt/secret_rotation.py.
# Plaintext on purpose (names + dates + tiers only, never values); lives outside vars/ so
# SOPS does not encrypt it. Run `secret_rotation.py sync` after adding/removing a secret.
# You MAY edit a `tier` to override classification (sync preserves it); don't hand-edit
# `last_rotated` — `record` sets it to a date only the operator knows (a rename's
# carry-over, an app-side rotation), and `audit` reads the real date out of the git history
# of secrets.yml when a value changed later than this file records.
# Tiers: assisted|external|external-90d|pinned|ignore. `audit --check` refuses any other.
# `source: record` marks a key whose value SOPS only RECORDS — the app holds the credential and
# nothing in the tree writes it there, so `sops set` alone rotates nothing and the deploy reads
# green either way. Change the credential in the app first, then record it here.
# docs/secret-rotation.md names each key's mechanism. sync preserves the field.
"""


def save_registry(reg: dict, path: str = REGISTRY_FILE) -> None:
    """Write `reg` back to the registry, managed header first and keys sorted."""
    body = yaml.safe_dump(reg, sort_keys=True, default_flow_style=False)
    with open(path, "w") as fh:
        fh.write(_HEADER)
        fh.write(body)


# The rotation cadence per tier, and the default `secret_registry.py` seeds, syncs and audits with.
# `secret_rotation.py` assigns the same table AS A LITERAL, because
# `scripts/docs/gen_doc_fragments.py` reads it out of THAT file with `ast.literal_eval` and a
# leaf may not import its own facade. The two copies are held equal by
# `test_the_default_tier_table_is_the_one_secret_rotation_assigns`. A MappingProxyType rather
# than a dict, because a dataclass rejects a mutable default and this object is frozen.
DEFAULT_TIER_DAYS = MappingProxyType(
    {
        "assisted": 365,
        "external": 365,
        "external-90d": 90,
        "pinned": 730,
        "ignore": None,
    }
)


@dataclass(frozen=True)
class RotationTools:
    """Every boundary the rotation tool crosses, so a test replaces a field not a module."""

    git: Callable[..., str] = run_git
    today: Callable[[], dt.date] = today
    load_registry: Callable[..., dict] = load_registry
    save_registry: Callable[..., None] = save_registry
    sops_names: Callable[..., list[str]] = sops_names
    tier_days: Mapping[str, int | None] = DEFAULT_TIER_DAYS
