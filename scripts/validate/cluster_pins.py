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

The key set anywhere else under `ansible/` is refused: inventory, play vars, `include_role`
vars and `set_fact` override a role default, so a pin there would move the deployed version
while the role file stays put. That scan matches the key at any indent, as YAML `key:` or as
INI `key=`, in every YAML and INI file and every file under `inventory/`, `group_vars/` or
`host_vars/`. A `-e` passed at run time is outside its reach.

There is no bypass. The plan is docs/target-architecture/host/h1-cluster-config-to-flux.md,
*CI checks on the pins*, and docs/landing.md says what to do with a refused PR.

- **A k3s downgrade.** `k3s_version` is a forward-only pin of its own kind (H6.3c). A decrease
  is refused unless `k3s_downgrade_acknowledged` in the same file equals the new pin, and a
  raise must leave that key absent. The acknowledged decrease is the H6.6 rollback, which
  lowers the pin after a hand downgrade. `k3s_version` is not in PINS because a bump of it
  legitimately changes the vendored schemas and other roles, which the alone rule would refuse.

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
# YAML only: a role's CLAUDE.md or template can quote `<key>: v1.2.3` at a line start.
ANSIBLE_YAML = (":(glob)ansible/**/*.yml", ":(glob)ansible/**/*.yaml")
# Every file Ansible reads variables from. Inventory files take any extension or none.
ANSIBLE_VARS = (
    *ANSIBLE_YAML,
    ":(glob)ansible/**/*.ini",
    ":(glob)ansible/inventory/**",
    ":(glob)ansible/**/group_vars/**",
    ":(glob)ansible/**/host_vars/**",
)
ROLES = "ansible/roles/"
K3S_DEFAULTS = "ansible/roles/k8s/system-upgrade/defaults/main.yml"
K3S_KEY, K3S_ACK_KEY = "k3s_version", "k3s_downgrade_acknowledged"
K3S_VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)\+k3s(\d+)")
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


def k3s_problems(old: str | None, new: str | None, ack: str | None) -> list[str]:
    """Each refusal for a `k3s_version` move from `old` to `new`, given the head's `ack` value.

    Raises:
        Unjudged: a version is not `vX.Y.Z+k3sN`.
    """
    if new is None:
        if old is None:
            return []
        return [
            f"{K3S_DEFAULTS}: {K3S_KEY} is gone at the head, so no later PR can be "
            "checked against it"
        ]
    if old is None or old == new:
        return []
    parsed = []
    for value in (old, new):
        match = K3S_VERSION.fullmatch(value)
        if not match:
            raise Unjudged(f"{K3S_DEFAULTS}: {value!r} is not a vX.Y.Z+k3sN version")
        parsed.append(tuple(int(g) for g in match.groups()))
    if parsed[1] < parsed[0]:
        if ack == new:
            return []
        return [
            f"{K3S_DEFAULTS}: {K3S_KEY} decreases from {old} to {new}. SUC's upgrade.sh "
            f"refuses an older binary, so set {K3S_ACK_KEY}: {new} in the same "
            "file after the hand downgrade (docs/k3s-upgrade.md, Rollback)"
        ]
    if ack is not None:
        return [
            f"{K3S_DEFAULTS}: {K3S_KEY} rises from {old} to {new} while {K3S_ACK_KEY} "
            "is set. Remove the key in the bump"
        ]
    return []


def k3s_values_at(sha: str, repo: Path = REPO) -> tuple[str | None, str | None]:
    """`k3s_version` and `k3s_downgrade_acknowledged` at `sha`; None where the file or key is absent."""
    shown = git("show", f"{sha}:{K3S_DEFAULTS}", cwd=repo, check=False)
    if shown.returncode != 0:
        return None, None
    data = yaml_fast.safe_load(shown.stdout)
    if not isinstance(data, dict):
        raise Unjudged(f"{K3S_DEFAULTS} is not a mapping at {sha[:9]}")
    values = []
    for key in (K3S_KEY, K3S_ACK_KEY):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise Unjudged(f"{K3S_DEFAULTS}: {key} is not a string at {sha[:9]}")
        values.append(value)
    return values[0], values[1]


def files_setting(key: str, sha: str, repo: Path = REPO) -> list[str]:
    """Every YAML file under `ansible/` that sets `key` at the top level at `sha`."""
    hits = git(
        "grep", "-l", "-E", f"^{key}:", sha, "--", *ANSIBLE_YAML, cwd=repo, check=False
    )
    if hits.returncode not in (0, 1):
        raise Unjudged(f"git grep for {key} at {sha[:9]} failed: {hits.stderr}")
    return [line.split(":", 1)[1] for line in hits.stdout.splitlines()]


# A quote opens at the line start or after whitespace or `{[,:=`, as a YAML scalar or an INI
# value Ansible splits with shlex does. A quote inside a bare word, as in `x's`, opens none.
QUOTE_OPENERS = " \t{[,:="


def code_part(line: str) -> str:
    """`line` with its comment cut off, so a key quoted in the comment is not read.

    A comment starts at an unquoted `#` that opens the line or follows whitespace (YAML, and
    INI), or at a `;` that opens the line (INI).

    A `#` or `;` inside a value, as in `{note: "see #1", ...}`, `note="a #b"` or
    `ansible_user=x;y`, starts none, so an assignment after it still counts. An unclosed quote
    keeps the rest of the line, which errs toward reading an assignment.
    """
    if line.lstrip().startswith(";"):
        return ""
    quote, escaped = "", False
    for i, char in enumerate(line):
        if quote:
            # A double-quoted YAML scalar escapes with `\`, a single-quoted one with `''`.
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote == "'" and line[i + 1 : i + 2] == "'":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'" and (i == 0 or line[i - 1] in QUOTE_OPENERS):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def assigns(key: str, line: str) -> bool:
    """Whether `line` assigns `key` at any indent: YAML `key:`, a flow `{key: v}`, INI `key=v`.

    `==` is no assignment, so a `when: <key> == 'v1.2.3'` comparison is not one, and neither
    is a key quoted in the line's comment.
    """
    return bool(
        re.search(rf"""(^|[\s{{,"'-]){key}["']?\s*(:|=(?!=))""", code_part(line))
    )


def files_assigning(key: str, sha: str, repo: Path = REPO) -> list[str]:
    """Every file under `ansible/` that Ansible reads vars from and that assigns `key` at `sha`."""
    hits = git("grep", "-n", "-F", key, sha, "--", *ANSIBLE_VARS, cwd=repo, check=False)
    if hits.returncode not in (0, 1):
        raise Unjudged(f"git grep for {key} at {sha[:9]} failed: {hits.stderr}")
    found: dict[str, None] = {}
    for hit in hits.stdout.splitlines():
        _sha, path, _lineno, line = hit.split(":", 3)
        if assigns(key, line):
            found[path] = None
    return list(found)


def overrides(sha: str, repo: Path = REPO) -> list[str]:
    """A refusal for each file at `sha` that assigns a pin, other than the role file holding it.

    Inventory, play vars and task vars take precedence over role defaults, so a pin set there
    would move the deployed version while the role file, which `pins_at` reads, stays the same.
    """
    found: list[str] = []
    for key in PINS:
        holders = {f for f in files_setting(key, sha, repo) if f.startswith(ROLES)}
        found += [
            f"{path}: {key} is set outside the role file that holds the pin, where it "
            "would override the role's pin unchecked. Set it in that role file only"
            for path in files_assigning(key, sha, repo)
            if path not in holders
        ]
    return found


def pins_at(sha: str, repo: Path = REPO) -> dict[str, Pin | None]:
    """Each key in PINS mapped to its pin at `sha`, read from whichever role file holds it."""
    found: dict[str, Pin | None] = {}
    for key in PINS:
        files = [f for f in files_setting(key, sha, repo) if f.startswith(ROLES)]
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
        old_k3s, _ = k3s_values_at(base, repo)
        new_k3s, ack = k3s_values_at(head, repo)
        found = (
            overrides(head, repo)
            + problems(pins_at(base, repo), pins_at(head, repo), paths)
            + k3s_problems(old_k3s, new_k3s, ack)
        )
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
