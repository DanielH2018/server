#!/usr/bin/env python3
"""Refuse a PR that moves a forward-only cluster pin the wrong way (H1.1f).

After H1, merging a Longhorn or MetalLB pin is the upgrade, and neither upgrade can run
backwards. This check compares each pin's value at the PR's base and head and refuses three
shapes:

- **A decrease.** Any pin whose head value is lower than its base value, so a revert PR of a
  bump fails CI. A pin moves back in a forward PR that keeps the version, never a revert.
- **A skipped Longhorn minor.** Longhorn upgrades one minor release at a time, so
  `v1.12.1` to `v1.14.0` is refused, and `v1.12.1` to `v1.13.0` or `v1.12.2` is not. A major
  release counts as a skip too: the version numbers cannot say which minor it follows, so a
  major bump is a decision for the operator, not for this check.
- **A bump that shares its PR.** A PR that bumps a pin may change only the role directory
  that holds the pin, which carries its vendored file, and docs. A second pin's bump counts
  as sharing too. `land.sh`'s land gate (H1.1g) relies on this: it gates one bump per PR.

A pin changes when its value changes. The check reads each pin from whichever file under
`ansible/roles/` holds the key at each SHA, so a PR that moves the key into another role at
the same version is no bump, and neither the decrease nor the alone check applies to it.

There is no bypass. The plan is docs/target-architecture/host/h1-cluster-config-to-flux.md,
*CI checks on the pins*, and docs/landing.md says what to do with a refused PR.

Exit codes: 0 clean; 1 refused; 2 a pin could not be read, so the check could not judge.

Run: uv run python scripts/validate/cluster_pins.py --base origin/master
     uv run pytest scripts/validate/tests/test_validate_cluster_pins.py
"""

import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.git import git, git_stdout
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

# The deployer's library is imported by file path, not as a package.
if str(GITOPS_DEPLOY_FILES) not in _sys.path:
    _sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_changes import is_doc

CLEAN, REFUSED, UNJUDGED = 0, 1, 2

# The forward-only pins, each with whether a bump may move it by at most one minor release.
# MetalLB has no such rule upstream, so only its decrease and its PR's other paths are checked.
PINS: Mapping[str, bool] = {
    "k3s_longhorn_version": True,
    "k3s_metallb_version": False,
}
# Role YAML only: a role's CLAUDE.md or template can quote `<key>: v1.2.3` at a line start.
ROLE_YAML = (":(glob)ansible/roles/**/*.yml", ":(glob)ansible/roles/**/*.yaml")
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class Unjudged(Exception):
    """A pin's file or value cannot be read, so no verdict is possible."""


@dataclass(frozen=True)
class Pin:
    """One pin's value at one SHA, and the file that holds it."""

    path: str
    value: str

    @property
    def role_dir(self) -> str:
        """The role directory holding the pin, `ansible/roles/<tree>/<role>/`."""
        return "/".join(self.path.split("/")[:4]) + "/"


def version(pin: Pin) -> tuple[int, int, int]:
    """The pin's value as `(major, minor, patch)`; anything but `vX.Y.Z` is unjudged."""
    match = VERSION.fullmatch(pin.value)
    if not match:
        raise Unjudged(f"{pin.path}: {pin.value!r} is not a vX.Y.Z version")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def problems(
    base: Mapping[str, Pin | None],
    head: Mapping[str, Pin | None],
    paths: Iterable[str],
) -> list[str]:
    """Each refusal for one PR, as `<path>: <reason>` lines.

    Args:
        base: each key in PINS mapped to its pin at the base, or None where no file holds it.
        head: the same at the head.
        paths: the paths the PR adds, modifies or deletes.

    Raises:
        Unjudged: a pin's value is not a version.
    """
    found: list[str] = []
    bumped: dict[str, Pin] = {}
    for key, minor_steps in PINS.items():
        old, new = base.get(key), head.get(key)
        if new is None:
            if old is not None:
                found.append(
                    f"{old.path}: {key} is gone at the head, so no later PR can be "
                    "checked against it"
                )
            continue
        if old is None or old.value == new.value:
            continue
        was, now = version(old), version(new)
        if now < was:
            found.append(
                f"{new.path}: {key} decreases from {old.value} to {new.value}. "
                "Move the pin back in a forward PR instead"
            )
            continue
        bumped[key] = new
        if minor_steps and (now[0] != was[0] or now[1] - was[1] > 1):
            found.append(
                f"{new.path}: {key} moves from {old.value} to {new.value}, which skips a "
                "minor release. Bump one minor at a time"
            )
    if len(bumped) > 1:
        found.append(
            f"{next(iter(bumped.values())).path}: {' and '.join(bumped)} both move in one "
            "PR. Bump each pin in a PR of its own"
        )
    for key, pin in bumped.items():
        allowed = {pin.role_dir}
        if (old := base.get(key)) is not None:
            allowed.add(old.role_dir)
        for path in sorted(paths):
            if not is_doc(path) and not any(path.startswith(d) for d in allowed):
                found.append(
                    f"{path}: the PR bumps {key}, so it may change only "
                    f"{' and '.join(sorted(allowed))} and docs"
                )
    return found


def pins_at(sha: str, repo: Path = REPO) -> dict[str, Pin | None]:
    """Each key in PINS mapped to its pin at `sha`, read from whichever role file holds it."""
    found: dict[str, Pin | None] = {}
    for key in PINS:
        hits = git(
            "grep", "-l", "-E", f"^{key}:", sha, "--", *ROLE_YAML, cwd=repo, check=False
        )
        if hits.returncode not in (0, 1):
            raise Unjudged(f"git grep for {key} at {sha[:9]} failed: {hits.stderr}")
        files = [line.split(":", 1)[1] for line in hits.stdout.splitlines()]
        if len(files) > 1:
            raise Unjudged(f"{key} is set in {len(files)} files at {sha[:9]}: {files}")
        if not files:
            found[key] = None
            continue
        data = yaml_fast.safe_load(git_stdout("show", f"{sha}:{files[0]}", cwd=repo))
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, str):
            raise Unjudged(f"{files[0]}: {key} is not a string at {sha[:9]}")
        found[key] = Pin(files[0], value)
    return found


def check(base: str, head: str, repo: Path = REPO) -> int:
    """Judge `base..head` in `repo`, each side's pins read from its own tree."""
    base, head = (
        git_stdout("rev-parse", "--verify", f"{r}^{{commit}}", cwd=repo)
        for r in (base, head)
    )
    paths = git_stdout(
        "diff", "--no-renames", "--name-only", base, head, cwd=repo
    ).split()
    try:
        found = problems(pins_at(base, repo), pins_at(head, repo), paths)
    except Unjudged as exc:
        print(f"[FAIL] cluster-pins: cannot judge. {exc}", file=sys.stderr)
        return UNJUDGED
    for line in found:
        print(f"[FAIL] {line}", file=sys.stderr)
    if not found:
        print(f"cluster-pins: {base[:9]}..{head[:9]} moves no pin the wrong way")
    return REFUSED if found else CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the PR's base commit")
    parser.add_argument(
        "--head", default="HEAD", help="the PR's head commit (default HEAD)"
    )
    args = parser.parse_args(argv)
    return check(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
