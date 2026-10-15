"""The two SOPS-facing helpers that import no other secrets_mgmt module.

`malformed_push_tokens` is the push-token shape check, over an already-decrypted mapping.
`RotationTools.sops_decrypt` does the decrypting; the check only inspects what it returned,
which is what lets it be tested with synthetic values on a host (or in CI) with no age key.
It opens no file and spawns no process.

`sops_set` writes one secret into a SOPS file through `sops set`, with the runner injectable.
It lives here, and not in `rotation_tools.py`, because both `rotation_tools.py` and
`cluster_vars.py` need it and each imports the other's work: a leaf both can import keeps the
import graph acyclic.
"""

import os
import re
import subprocess
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.repo_paths import REPO as _REPO_PATH

# `str`, because every consumer builds paths with os.path.join. lib.repo_paths.REPO is a Path;
# this module is the last of the twenty call sites that module's docstring describes, which
# re-derived `parents[2]` by hand under three different names.
REPO = str(_REPO_PATH)
SECRETS_FILE = os.path.join(REPO, "ansible", "vars", "secrets.yml")

# Uptime Kuma rejects a push token that is not exactly 32 letters/digits, and it rejects it at
# monitor-CREATION time — so the tile is never made and every repo-side gate stays green: the
# template interpolates the bad value fine, the manifest renders, CI passes, and the only
# evidence is a validation error in the autokuma sidecar log. `cmd_rotate` mints
# `token_hex(16)`, which satisfies this, so the auto-rotation path agrees.
PUSH_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def malformed_push_tokens(values: dict) -> list[tuple[str, str]]:
    """Every `*_push_token` whose value Uptime Kuma would refuse, as (name, reason).

    Reasons name the LENGTH and the offending character class, never the value — this feeds a
    Kuma push message and a stdout line, both of which are readable places.

    The `*_push_token` suffix is a complete selector, measured in both directions: every
    secret so named lands in a `push_token` field of uptime-kuma's static-monitors tile, a
    `KUMA_PUSH_*` env var, or an `/api/push/` URL; and every var interpolated into one of
    those three positions is so named (62 of each, no exceptions). A Kuma push token added
    under a different name would be invisible here — grep those three positions if this ever
    needs rechecking.
    """
    bad = []
    for name, value in sorted(values.items()):
        if not name.endswith("_push_token"):
            continue
        if not isinstance(value, str):
            bad.append((name, "not a string"))
        elif not PUSH_TOKEN_RE.match(value):
            bad.append(
                (
                    name,
                    "%d chars, wants 32 letters/digits" % len(value)
                    if len(value) != 32
                    else "32 chars but has a non-alphanumeric character",
                )
            )
    return bad


def sops_set(
    name: str,
    value: str,
    *,
    path: str = SECRETS_FILE,
    run: Callable = subprocess.run,
) -> None:
    """Write `value` as secret `name`'s plaintext in the SOPS file `path`, on stdin.

    --value-stdin keeps the new token out of argv (world-readable via /proc/<pid>/cmdline
    here — no hidepid). It still requires a JSON-encoded value, same as the old argv form, so
    the quoting stays; only the transport moves to stdin. `path` defaults to `secrets.yml`;
    `cluster_vars.py write` passes the cluster file.
    """
    run(
        ["sops", "set", "--value-stdin", path, '["%s"]' % name],
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
