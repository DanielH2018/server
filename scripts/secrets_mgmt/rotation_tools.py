"""Every process boundary `secret_rotation.py` crosses, as one injectable object.

A test replaces one field of `RotationTools` and never a module attribute. The defaults are
the real implementations, defined HERE rather than beside their callers: `secret_rotation.py`
is the entry point, so a default imported from it would be a cycle — and, when that file runs
as `__main__`, a second copy of the module under a second name. **This module names
`secret_rotation` nowhere**, at import time or later, which is what makes it a leaf every
other module in the package can import. The one fact both files hold is the tier table, and
each spells the literal out; `DEFAULT_TIER_DAYS` below says why, and which test holds the two
equal.

WHY THE REAL IMPLEMENTATIONS TAKE A `run` KEYWORD. `sops_set` and `decrypted_values` build
an argv whose exact shape is the security property: a freshly minted token travels on stdin,
never in `/proc/<pid>/cmdline` (the 2026-08-27 fix). A test that replaces the whole field
with a fake stops checking that argv. Injecting the runner instead lets a test drive the REAL
builder and keep asserting what it hands the process.
"""

import contextlib
import datetime as dt
import os
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from zoneinfo import ZoneInfo

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting. `sys` is spelled
# `_sys` throughout so `run_deploy` below shares the one import the bootstrap needs.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib import yaml_fast
from lib.git import git

REPO = str(_Path(__file__).resolve().parents[2])
SECRETS_FILE = os.path.join(REPO, "ansible", "vars", "secrets.yml")
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
# `last_rotated` — `rotate` updates it, and `audit` reads the real date out of the git
# history of secrets.yml when a value changed later than this file records.
# Tiers: auto|assisted|external|pinned|ignore.
"""


def save_registry(reg: dict, path: str = REGISTRY_FILE) -> None:
    """Write `reg` back to the registry, managed header first and keys sorted."""
    body = yaml.safe_dump(reg, sort_keys=True, default_flow_style=False)
    with open(path, "w") as fh:
        fh.write(_HEADER)
        fh.write(body)


def sops_set(name: str, value: str, *, run: Callable = subprocess.run) -> None:
    """Write `value` as secret `name`'s plaintext, handing it over on stdin.

    --value-stdin keeps the new token out of argv (world-readable via /proc/<pid>/cmdline
    here — no hidepid). It still requires a JSON-encoded value, same as the old argv form, so
    the quoting stays; only the transport moves to stdin.
    """
    run(
        ["sops", "set", "--value-stdin", SECRETS_FILE, '["%s"]' % name],
        input='"%s"' % value,
        text=True,
        check=True,
        cwd=REPO,
        # A hung `sops set` would otherwise hang the weekly secret-rotate cron forever,
        # mid-batch, still holding the repo-tree lock the deploy pipeline shares. Bounded
        # the same as the sibling `decrypted_values`; `cmd_rotate` catches the
        # TimeoutExpired alongside its other failures, so the names already written to the
        # store are still reported.
        timeout=30,
    )


def decrypted_values(
    path: str = SECRETS_FILE, *, run: Callable = subprocess.run
) -> dict | None:
    """Plaintext secrets, or None when this host cannot decrypt (no age key — e.g. CI).

    None is a legitimate answer, not an error: the audit's other arms are deliberately
    decrypt-free so they run in CI, and this one simply has nothing to say there. Nothing from
    the subprocess is echoed on failure — stdout holds the plaintext, so putting it in a
    message or a traceback is the one way this helper could leak.
    """
    try:
        r = run(
            ["sops", "--decrypt", path],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO,
            # A hung decrypt would otherwise hang the daily cron and the prek gate, which
            # runs `audit` on every commit touching the store, the registry or this file.
            timeout=30,
        )
        data = yaml_fast.safe_load(r.stdout) or {}
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        yaml.YAMLError,
    ):
        return None
    return {k: v for k, v in data.items() if k != "sops"}


def kuma_push(url: str, ok: bool, msg: str) -> None:
    """Post one up/down beat to an Uptime Kuma push monitor."""
    full = "%s?status=%s&msg=%s" % (
        url,
        "up" if ok else "down",
        urllib.parse.quote(msg),
    )
    urllib.request.urlopen(full, timeout=10).read()


def run_deploy(tags: list[str], *, run: Callable = subprocess.run) -> int:
    """Redeploy the rotated secrets' consumers; returns the playbook's exit code."""
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
    # Ansible exits at import on a non-blocking stdout or stderr, and Claude Code's
    # Bash tool hands its child both with O_NONBLOCK set. The flag lives on the open
    # file description this process shares with the child, so clearing it here clears
    # it for ansible; anywhere else it is already clear and this does nothing. The
    # .claude/hooks/uv-python.sh fixup cannot reach here — this command names ansible
    # nowhere a hook reading the session's command text could see it.
    for handle in (_sys.stdin, _sys.stdout, _sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            os.set_blocking(handle.fileno(), True)
    return run(cmd, cwd=REPO).returncode


# The rotation cadence per tier, and the default `secret_registry.py` seeds, syncs and audits with.
# `secret_rotation.py` assigns the same table AS A LITERAL, because
# `scripts/docs/gen_doc_fragments.py` reads it out of THAT file with `ast.literal_eval` and a
# leaf may not import its own facade. The two copies are held equal by
# `test_the_default_tier_table_is_the_one_secret_rotation_assigns`. A MappingProxyType rather
# than a dict, because a dataclass rejects a mutable default and this object is frozen.
DEFAULT_TIER_DAYS = MappingProxyType(
    {"auto": 180, "assisted": 365, "external": 365, "pinned": 730, "ignore": None}
)


@dataclass(frozen=True)
class RotationTools:
    """Every boundary the rotation tool crosses, so a test replaces a field not a module."""

    git: Callable[..., str] = run_git
    today: Callable[[], dt.date] = today
    load_registry: Callable[..., dict] = load_registry
    save_registry: Callable[..., None] = save_registry
    sops_names: Callable[..., list[str]] = sops_names
    sops_decrypt: Callable[..., dict | None] = decrypted_values
    sops_set: Callable[[str, str], None] = sops_set
    kuma_push: Callable[[str, bool, str], None] = kuma_push
    deploy: Callable[[list[str]], int] = run_deploy
    tier_days: Mapping[str, int | None] = DEFAULT_TIER_DAYS
