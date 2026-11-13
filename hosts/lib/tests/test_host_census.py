"""Every directory under `hosts/` that is a host appears in each set naming hosts (H7.15d).

The rest of `docs/host-packages.md`, *Adding a host*, is one edit per set, and a set missing a host
fails nothing until that host's first rollout, or never. `census_problems` reads the sets from a
tree's files, so the fixture below can add a host to every set but one.

Run: uv run pytest hosts/lib/tests/test_host_census.py
"""

import ast
import re
import shutil
from pathlib import Path

import pytest
from lib import yaml_fast
from lib.repo_paths import REPO

NOT_HOSTS = frozenset({"components", "keys", "lib", "packages", "tests", "__pycache__"})
KNOWN_HOSTS = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
ROOTS = "scripts/validate/validate_lib/package_roots.py"
GATE = "hosts/lib/builder_rollout_gate.py"
SCRIPT = "hosts/bootstrap-host.sh"
SOPS = "ansible/.sops.yaml"
NEW_HOST = "daniel-new"


def host_dirs(tree: Path) -> set[str]:
    return {
        path.name
        for path in (tree / "hosts").iterdir()
        if path.is_dir() and path.name not in NOT_HOSTS
    }


def _python_tuple(path: Path, name: str) -> set[str]:
    """The literal tuple the module at `path` assigns to `name`."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{path} assigns no {name}")


def _script_hosts(path: Path) -> set[str]:
    found = re.search(r'^HOSTS="(.*)"$', path.read_text(), re.MULTILINE)
    assert found, f"{path} sets no HOSTS"
    return set(found[1].split())


def has_own_creation_rule(rules: list[dict], host: str) -> bool:
    """Whether the first rule matching `host`'s SOPS file is one that matches no other host's.

    SOPS takes the first match, and the generic rule matches every per-host path too, so a rule
    placed after it never applies.
    """
    path = f"vars/hosts/{host}.sops.yaml"
    for rule in rules:
        if re.search(rule["path_regex"], path):
            return not re.search(
                rule["path_regex"], "vars/hosts/no-such-host.sops.yaml"
            )
    return False


def census_problems(tree: Path) -> list[str]:
    """One line per host and set that lacks it, naming the set."""
    named = _python_tuple(tree / ROOTS, "NAMED")
    canary = _python_tuple(tree / GATE, "CANARY_ORDER") | _python_tuple(
        tree / GATE, "NEVER_CANARY"
    )
    script = _script_hosts(tree / SCRIPT)
    rules = yaml_fast.safe_load((tree / SOPS).read_text())["creation_rules"]
    problems = []
    for host in sorted(host_dirs(tree)):
        if f"homelab-{host}" not in named:
            problems.append(f"{host}: homelab-{host} is missing from NAMED in {ROOTS}")
        if host not in canary:
            problems.append(
                f"{host}: missing from CANARY_ORDER and NEVER_CANARY in {GATE}"
            )
        if host not in script:
            problems.append(f"{host}: missing from HOSTS in {SCRIPT}")
        if not has_own_creation_rule(rules, host):
            problems.append(f"{host}: no creation rule of its own in {SOPS}")
    return problems


def _rule(host: str) -> str:
    return f'  - path_regex: vars/hosts/{host}\\.sops\\.yaml$\n    age: "age1new"\n'


def _edit_sub(tree: Path, relative: str, pattern: str, replacement: str) -> None:
    path = tree / relative
    changed, count = re.subn(
        pattern, replacement, path.read_text(), count=1, flags=re.MULTILINE
    )
    assert count == 1, f"{relative} no longer holds {pattern}"
    path.write_text(changed)


GENERIC_RULE = r"^  - path_regex: \.\*\(vars\|secrets\)"


@pytest.fixture
def fixture_tree(tmp_path) -> Path:
    """A copy of the four files with NEW_HOST added to `hosts/`, and to no set yet."""
    for relative in (ROOTS, GATE, SCRIPT, SOPS):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, tmp_path / relative)
    for host in KNOWN_HOSTS | {NEW_HOST}:
        (tmp_path / "hosts" / host).mkdir()
    return tmp_path


def _add_named(tree: Path) -> None:
    _edit_sub(tree, ROOTS, r"^NAMED = \(\n", f'NAMED = (\n    "homelab-{NEW_HOST}",\n')


def _add_canary(tree: Path) -> None:
    _edit_sub(tree, GATE, r"^CANARY_ORDER = \(", f'CANARY_ORDER = ("{NEW_HOST}", ')


def _add_script(tree: Path) -> None:
    _edit_sub(tree, SCRIPT, r'^HOSTS="', f'HOSTS="{NEW_HOST} ')


def _add_rule(tree: Path) -> None:
    _edit_sub(
        tree, SOPS, GENERIC_RULE, _rule(NEW_HOST) + "  - path_regex: .*(vars|secrets)"
    )


ADDS = {"NAMED": _add_named, "CANARY_ORDER": _add_canary, "HOSTS": _add_script}


def test_the_tree_is_clean():
    assert KNOWN_HOSTS <= host_dirs(REPO), "the census no longer finds the fleet"
    assert census_problems(REPO) == []


def test_a_host_in_every_set_is_clean(fixture_tree):
    for add in ADDS.values():
        add(fixture_tree)
    _add_rule(fixture_tree)

    assert census_problems(fixture_tree) == []


@pytest.mark.parametrize("left_out", sorted(ADDS))
def test_a_host_missing_from_a_set_is_flagged_and_the_set_named(fixture_tree, left_out):
    for name, add in ADDS.items():
        if name != left_out:
            add(fixture_tree)
    _add_rule(fixture_tree)

    problems = census_problems(fixture_tree)

    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{NEW_HOST}: ")
    assert left_out in problems[0]


def test_a_host_with_no_creation_rule_is_flagged(fixture_tree):
    for add in ADDS.values():
        add(fixture_tree)

    assert census_problems(fixture_tree) == [
        f"{NEW_HOST}: no creation rule of its own in {SOPS}"
    ]


def test_a_creation_rule_after_the_generic_rule_is_flagged(fixture_tree):
    for add in ADDS.values():
        add(fixture_tree)
    sops = fixture_tree / SOPS
    sops.write_text(sops.read_text().rstrip("\n") + "\n" + _rule(NEW_HOST))

    assert census_problems(fixture_tree) == [
        f"{NEW_HOST}: no creation rule of its own in {SOPS}"
    ]
