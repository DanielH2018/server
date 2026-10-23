#!/usr/bin/env python3
"""Per-host SOPS files: `ansible/vars/hosts/<host>.sops.yaml`, each copied from secrets.yml.

Slice H4 ships one SOPS file per host inside the host's package. The host decrypts it with
its own age key, so no host except the ones that run Ansible ever holds a key for
`secrets.yml` (ADR-0022). Each file holds only the keys that host's rendered tree names, and
every value in it is a copy of the one in `secrets.yml`. `secrets.yml` stays the only source.

  check — keep each copy in step. It decrypts nothing and needs no key, so it runs in the
          pytest job. It fails when:
            - a file's key names differ from the keys the host's tree names (`package.yml`
              `credentials:` and each `outputs:` entry's `keys:`, in the package and in its
              components), or a host whose tree names keys has no file;
            - a value is not SOPS ciphertext;
            - for a key the file carries, the newest commit that changed it in the host file
              does not descend from, or equal, the newest commit that changed it in
              `secrets.yml` (`git merge-base --is-ancestor`, so two commits on one day still
              order), or git returns no commit for it;
            - the clone is shallow, because a depth-1 walk dates every key at one commit and
              would pass the comparison.
  sync <host> — OPERATOR-ONLY. Decrypts `secrets.yml`, then rewrites `<host>`'s file whole
          with the keys its tree names, encrypted by the creation rule for that path in
          `ansible/.sops.yaml`. Run it in the same PR as the change that adds a key to a
          host's tree or rotates a key a host file carries; `check` in CI is the gate and no
          cron calls it. It rewrites every value on purpose: the host-file walk in
          `git_dates.py` reads that rewrite as the copy.

Exit codes: 0 clean or written; 1 a check failed, or sync could not complete; 64 a usage error.

Run: uv run python scripts/secrets_mgmt/host_secrets.py check
     cd /home/ubuntu/server && uv run python scripts/secrets_mgmt/host_secrets.py sync daniel-server
"""

import os
import re
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/

import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.repo_paths import REPO
from secrets_mgmt.cluster_vars import SopsFailed, decrypt, encrypt
from secrets_mgmt.cluster_vars_check import Tree
from secrets_mgmt.git_dates import SECRETS_GIT_PATH, ciphertext_rotation_commits
from secrets_mgmt.rotation_tools import RotationTools

HOSTS_GIT_DIR = "ansible/vars/hosts"
SUFFIX = ".sops.yaml"
# A host name is one plain path piece, which keeps a joined path under its directory.
_HOST = re.compile(r"[a-z][a-z0-9-]*")
_CIPHERTEXT = re.compile(r"ENC\[AES256_GCM,.*\]")
# The directories under hosts/ that are not a host's own.
_NOT_HOSTS = frozenset({"components", "packages", "lib", "keys"})


def host_files(root: Path) -> dict[str, Path]:
    """Host name to its SOPS file, for each `<host>.sops.yaml` present."""
    directory = root / HOSTS_GIT_DIR
    return {
        path.name.removesuffix(SUFFIX): path
        for path in sorted(directory.glob("*" + SUFFIX))
    }


def _load(path: Path) -> dict:
    data = yaml_fast.safe_load(path.read_text()) if path.is_file() else None
    return data if isinstance(data, dict) else {}


def _unit_keys(spec: dict) -> set[str]:
    """The keys one `package.yml` or `component.yml` names: its credentials and output keys."""
    keys = {str(k) for k in spec.get("credentials") or []}
    for output in spec.get("outputs") or []:
        if isinstance(output, dict):
            keys |= {str(k) for k in output.get("keys") or []}
    return keys


def tree_keys(root: Path, host: str) -> frozenset[str]:
    """The secret keys `hosts/<host>/package.yml` and its components name.

    These are the credentials `homelab-secrets` materialises and the keys each declared
    output's placeholders use (H4, *Secret-bearing files*). A host with no `package.yml`
    names none.
    """
    spec = _load(root / "hosts" / host / "package.yml")
    keys = _unit_keys(spec)
    for name in spec.get("components") or []:
        if isinstance(name, str) and _HOST.fullmatch(name):
            keys |= _unit_keys(
                _load(root / "hosts" / "components" / name / "component.yml")
            )
    return frozenset(keys)


def tree_hosts(root: Path) -> list[str]:
    """Every host that has a package spec, sorted."""
    return sorted(
        path.parent.name
        for path in (root / "hosts").glob("*/package.yml")
        if path.parent.name not in _NOT_HOSTS
    )


def _key_problems(host: str, carried: set[str], named: frozenset[str]) -> list[str]:
    """Why a host file's key names differ from its tree's, as one line per key."""
    path = f"{HOSTS_GIT_DIR}/{host}{SUFFIX}"
    return [
        f"{path}: {key} is not named by hosts/{host}/ (remove it, or the tree lost it)"
        for key in sorted(carried - named)
    ] + [
        f"{path}: {key} is named by hosts/{host}/ and the file lacks it (run sync)"
        for key in sorted(named - carried)
    ]


def _plaintext_problems(host: str, path: Path) -> list[str]:
    """Each value of the file that is not an `ENC[...]` string. Names the key, never a value."""
    return [
        f"{HOSTS_GIT_DIR}/{host}{SUFFIX}: {key} is not SOPS ciphertext"
        for key, value in sorted(_load(path).items())
        if key != "sops"
        and not (isinstance(value, str) and _CIPHERTEXT.fullmatch(value))
    ]


def _descends(tools: RotationTools, ancestor: str, commit: str) -> bool:
    """Whether `commit` equals, or descends from, `ancestor`."""
    try:
        tools.git("merge-base", "--is-ancestor", ancestor, commit)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return False
        raise
    return True


def _freshness_problems(
    root: Path, files: dict[str, Path], tools: RotationTools
) -> list[str]:
    """Why a host file's copy of a key is older than `secrets.yml`'s value, or unknowable."""
    if tools.git("rev-parse", "--is-shallow-repository").strip() == "true":
        return [
            "the clone is shallow, so the walk would date every key at one commit and pass "
            "a stale copy: fetch full history (`git fetch --unshallow`)"
        ]
    source = ciphertext_rotation_commits(tools, SECRETS_GIT_PATH)
    out = []
    for host, path in files.items():
        rel = f"{HOSTS_GIT_DIR}/{host}{SUFFIX}"
        copies = ciphertext_rotation_commits(tools, rel)
        for key in sorted(k for k in _load(path) if k != "sops"):
            if key not in source:
                out.append(f"{rel}: {key} has no commit in {SECRETS_GIT_PATH}")
            elif key not in copies:
                out.append(f"{rel}: {key} has no commit in git (commit the file)")
            elif not _descends(tools, source[key], copies[key]):
                out.append(
                    f"{rel}: {key} is older than {SECRETS_GIT_PATH}'s value (run sync)"
                )
    return out


def check(root: Path, tools: RotationTools) -> list[str]:
    """Every way the host files disagree with their trees or with `secrets.yml`.

    Raises:
        subprocess.CalledProcessError: git failed on something other than the answer
            `--is-ancestor` gives.
    """
    files = host_files(root)
    problems = []
    for host in sorted(set(files) | set(tree_hosts(root))):
        named = tree_keys(root, host)
        if host not in files:
            if named:
                problems.append(
                    f"{HOSTS_GIT_DIR}/{host}{SUFFIX} is missing, and hosts/{host}/ names "
                    f"{len(named)} key(s) (run sync)"
                )
            continue
        if not (root / "hosts" / host / "package.yml").is_file():
            problems.append(
                f"{HOSTS_GIT_DIR}/{host}{SUFFIX}: hosts/{host}/package.yml does not exist"
            )
        carried = {k for k in _load(files[host]) if k != "sops"}
        problems += _key_problems(host, carried, named)
        problems += _plaintext_problems(host, files[host])
    if files:
        problems += _freshness_problems(root, files, tools)
    return problems


def sync(root: Path, host: str, run=subprocess.run) -> int:
    """Rewrite `host`'s file from `secrets.yml`; returns an exit code. Operator-only.

    The plaintext reaches `sops` on stdin and never an argv or a file. A failure leaves the
    old file in place, because the new one is written through a temporary file and renamed.
    """
    if not _HOST.fullmatch(host):
        print(f"sync: {host!r} is not a plain host name", file=sys.stderr)
        return FAILED
    named = sorted(tree_keys(root, host))
    if not named:
        print(
            f"sync: hosts/{host}/ names no secret key, so there is nothing to copy",
            file=sys.stderr,
        )
        return FAILED
    tree = Tree(root)
    target = root / HOSTS_GIT_DIR / f"{host}{SUFFIX}"
    try:
        values = decrypt(tree, tree.secrets, run)
        missing = [k for k in named if k not in values]
        if missing:
            print(
                f"sync: secrets.yml lacks {', '.join(missing)}, which hosts/{host}/ names",
                file=sys.stderr,
            )
            return FAILED
        text = yaml.safe_dump({k: values[k] for k in named}, default_flow_style=False)
        ciphertext = encrypt(tree, text, target, run)
    except (SopsFailed, ValueError) as exc:
        # SopsFailed names the file, never what sops printed. A ValueError from the JSON
        # parse could quote the decrypted text, so only its type is printed.
        print(
            f"sync: {exc if isinstance(exc, SopsFailed) else type(exc).__name__}",
            file=sys.stderr,
        )
        return FAILED
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(ciphertext)
    os.replace(tmp, target)
    print(f"sync: wrote {len(named)} key(s) to {target.relative_to(root)}")
    return OK


def main(
    argv: list[str] | None = None, root: Path = REPO, tools=None, run=subprocess.run
) -> int:
    """Parse `argv`, run the mode, and return its exit code."""
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser(
        "check", help="assert the host files against their trees and secrets.yml"
    )
    ps = sub.add_parser(
        "sync", help="operator-only: decrypt secrets.yml and rewrite one file"
    )
    ps.add_argument(
        "host", help="the host whose file to rewrite, such as daniel-server"
    )
    args = parser.parse_args(argv)
    if args.mode == "sync":
        return sync(root, args.host, run)
    try:
        problems = check(root, tools or RotationTools())
    except subprocess.CalledProcessError as exc:
        print(f"host_secrets check: git failed ({exc.cmd[:3]})", file=sys.stderr)
        return FAILED
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return FAILED
    print("host_secrets check: clean")
    return OK


if __name__ == "__main__":
    sys.exit(main())
