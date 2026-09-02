#!/usr/bin/env python3
"""Deployed host paths whose content embeds a credential, derived from the tree.

WHY THIS EXISTS. `grep-on-a-script-prints-the-secret-it-embeds` records three occurrences,
every one ending in a rotation. The leak each time was an *inspection*, not an execution: a
`grep -nE "rotate|--commit|sops set|push"` on `/usr/local/bin/secret-rotation-audit.sh` matched
the wrapper's own `export SECRET_ROTATION_KUMA=https://.../api/push/<token>` line and printed a
token minted an hour earlier into the terminal and the transcript.

WHY A DERIVATION RATHER THAN A LIST. `block-dangerous-bash.sh` guards reads by matching a fixed
alternation of conventional secret paths — `.env`, `.ssh/`, `id_rsa`. A deployed wrapper script
is not on that list and cannot be: its name is whatever the role called it. The set is knowable
from the tree, though, so it is measured rather than enumerated.

WHAT IT MEASURES. A task that deploys a file to a host bin directory (`template:`/`copy:` with a
`src` and a `dest` under /usr/local/bin or /opt/homelab) whose SOURCE references a secret name
from `ansible/secret_rotation.yml`. The registry is plaintext by design — names, dates and tiers,
never values — so this needs no SOPS key and runs anywhere, the same reasoning
`test_release_bin_groups_have_no_secrets.py` records for reading it.

WHAT IT DOES NOT MEASURE. A script that SOURCES a token from a separate env file (the kuma-push
pattern) holds no secret itself and is correctly absent; the env file it reads is already covered
by `block-dangerous-bash.sh`'s own `.env` arm. A secret reaching a host path by any route other
than a src/dest task — a shell `lineinfile`, a Job writing at runtime — is invisible here. This
narrows the class; it does not close it.

Run: uv run python scripts/secrets_mgmt/secret_bearing_host_paths.py
"""

import re
import sys as _sys
from pathlib import Path as _Path

import yaml

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import ANSIBLE  # noqa: E402

REGISTRY = ANSIBLE / "secret_rotation.yml"

# The trees a host script is deployed into. Deliberately two literal prefixes rather than "any
# absolute dest": a dest under /etc or a container's config directory is a data file, not
# something a session greps while deciding whether a script is safe to run, and widening this
# turns the guard into one that fires on ordinary config inspection.
HOST_BIN_PREFIXES = ("/usr/local/bin", "/opt/homelab")

# Names too generic to match on — a substring hit would flag every file mentioning the word.
# Empty today; kept as the documented escape hatch so a future generic name is handled here
# rather than by weakening the regex, matching test_release_bin_groups_have_no_secrets.py.
GENERIC_NAMES = frozenset()

# The registry's own word for "tracked, but not a value anyone rotates". `domain` carries it,
# and reading the tier is what keeps this derivation honest: matching on `domain` flagged five
# more host scripts (disk-health, etcd-snapshot-offbox, registry-gc, remember-logs-health,
# fake-remux-health) that embed no credential at all. A guard that fires on those is one that
# gets switched off. Derived from the registry rather than hand-listed in GENERIC_NAMES,
# because the registry already states the fact.
NON_SECRET_TIERS = frozenset({"ignore"})


def secret_names(registry: _Path = REGISTRY) -> list[str]:
    """Every tracked secret name, from the plaintext rotation registry.

    Excludes the `ignore` tier: those entries are tracked for completeness, not because they
    hold a value whose exposure forces a rotation.
    """
    data = yaml.safe_load(registry.read_text()) or {}
    tracked = {
        name
        for name, meta in (data.get("secrets") or {}).items()
        if (meta or {}).get("tier") not in NON_SECRET_TIERS
    }
    return sorted(tracked - GENERIC_NAMES)


def references_a_secret(text: str, names: list[str]) -> list[str]:
    """The secret names `text` references, as whole words.

    Whole-word, not substring: `authelia_secret` must not be found inside a comment about
    `authelia_secret_rotation`, and a short name must not match a longer unrelated identifier
    that happens to contain it.
    """
    return sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", text))


def _walk(node):
    """Yield every dict nested anywhere in a parsed task file."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _src_text(task_file: _Path, src: str) -> str:
    """The source file's text, resolved the way Ansible resolves a role-relative src.

    A `template:` src resolves against the role's templates/, a `copy:` src against its files/.
    Both are tried because the task's module name is not always adjacent to the dict this walk
    yields. An absolute or unresolvable src reads as empty, which is the fail-open direction:
    a source this cannot read is not evidence that it holds a secret.
    """
    role_dir = task_file.parent.parent
    for sub in ("templates", "files"):
        candidate = role_dir / sub / src
        if candidate.is_file():
            try:
                return candidate.read_text(errors="replace")
            except OSError:
                return ""
    return ""


def secret_bearing_host_paths(ansible: _Path = ANSIBLE) -> dict[str, list[str]]:
    """Deployed host path -> the secret names its source renders.

    Walks every role task file, not just k8s: `nut_host` and the `setup/` plane deploy host
    scripts too, and `setup/fake_remux` is the consumer `deploy.sh` structurally cannot reach.
    `archive/` is skipped — those roles deploy nothing.
    """
    names = secret_names()
    found: dict[str, list[str]] = {}
    for task_file in sorted(ansible.glob("roles/**/tasks/*.yml")):
        if "/archive/" in str(task_file):
            continue
        try:
            doc = yaml.safe_load(task_file.read_text(errors="replace"))
        except yaml.YAMLError:
            # A task file this cannot parse is not evidence of absence, but a census cannot
            # act on it either. Skipping one file beats failing the whole derivation.
            continue
        for node in _walk(doc):
            src, dest = node.get("src"), node.get("dest")
            if not isinstance(src, str) or not isinstance(dest, str):
                continue
            if not dest.startswith(HOST_BIN_PREFIXES):
                continue
            referenced = references_a_secret(_src_text(task_file, src), names)
            if referenced:
                found[dest] = referenced
    return found


def main() -> int:
    paths = secret_bearing_host_paths()
    for dest, names in sorted(paths.items()):
        print(f"{dest}\t{','.join(names)}")
    print(
        f"\n{len(paths)} deployed host paths embed a tracked secret.", file=_sys.stderr
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
