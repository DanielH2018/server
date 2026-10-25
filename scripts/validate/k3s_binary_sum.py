#!/usr/bin/env python3
"""Refuse a PR that moves `k3s_version` and leaves `k3s_binary_sha256_amd64` unchanged (H6.3e).

The `binary` mode of `hosts/lib/k3s_gates.py` compares each node's k3s binary with the sum in
`k3s_binary_sha256_amd64`, the `k3s` line of the release's `sha256sum-amd64.txt`. A bump of the
version that keeps the old sum would turn the verify Jobs red on every healthy node, or, worse,
get the sum "fixed" from the node. Both keys live in
`ansible/roles/k8s/system-upgrade/defaults/main.yml`. The sum may move alone (a correction).
A malformed sum is refused too: it must be 64 lowercase hex digits.

Exit codes: 0 clean; 1 refused; 2 a key could not be read, so the check could not judge.

Run: uv run python scripts/validate/k3s_binary_sum.py --base origin/master
     uv run pytest scripts/validate/tests/test_validate_k3s_binary_sum.py
"""

import re
import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.git import git_stdout
from lib.repo_paths import REPO, SYSTEM_UPGRADE_DEFAULTS

CLEAN, REFUSED, UNJUDGED = 0, 1, 2
VERSION_KEY, SUM_KEY = "k3s_version", "k3s_binary_sha256_amd64"
SUM_SHAPE = re.compile(r"[0-9a-f]{64}")


class Unjudged(Exception):
    """A key was absent or unreadable at one side."""


def problems(base: dict, head: dict) -> list[str]:
    """The refusals for a base and head pair of `{version, sum}` mappings."""
    found = []
    if not SUM_SHAPE.fullmatch(str(head[SUM_KEY])):
        found.append(f"{SUM_KEY} is not 64 lowercase hex digits")
    if head[VERSION_KEY] != base[VERSION_KEY] and head[SUM_KEY] == base[SUM_KEY]:
        found.append(
            f"{VERSION_KEY} moved {base[VERSION_KEY]} -> {head[VERSION_KEY]} and "
            f"{SUM_KEY} did not. Take the `k3s` line of that release's sha256sum-amd64.txt."
        )
    return found


def keys_at(sha: str, path: str, repo: Path, introducing: bool = False) -> dict:
    """The two keys at `sha`. The base side may lack the sum: the PR that adds it is clean."""
    try:
        data = yaml_fast.safe_load(git_stdout("show", f"{sha}:{path}", cwd=repo))
        if introducing and SUM_KEY not in data:
            return {VERSION_KEY: data[VERSION_KEY], SUM_KEY: None}
        return {k: data[k] for k in (VERSION_KEY, SUM_KEY)}
    except Exception as exc:
        raise Unjudged(f"{path} at {sha[:9]}: {exc!r}") from exc


def check(base: str, head: str, repo: Path = REPO, path: str = "") -> int:
    path = path or str(SYSTEM_UPGRADE_DEFAULTS.relative_to(REPO))
    base, head = (
        git_stdout("rev-parse", "--verify", f"{r}^{{commit}}", cwd=repo)
        for r in (base, head)
    )
    try:
        found = problems(keys_at(base, path, repo, True), keys_at(head, path, repo))
    except Unjudged as exc:
        print(f"[FAIL] k3s-binary-sum: cannot judge. {exc}", file=sys.stderr)
        return UNJUDGED
    for line in found:
        print(f"[FAIL] k3s-binary-sum: {line}", file=sys.stderr)
    if not found:
        print(
            f"k3s-binary-sum: {base[:9]}..{head[:9]} keeps the version and sum together"
        )
    return REFUSED if found else CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the PR's base commit")
    parser.add_argument("--head", default="HEAD", help="the PR's head (default HEAD)")
    args = parser.parse_args(argv)
    return check(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
