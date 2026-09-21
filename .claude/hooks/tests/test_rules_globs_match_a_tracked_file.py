"""Every `.claude/rules/*.md` has a `paths:` glob that matches at least one tracked file.

A rule whose globs match nothing parses fine, loads never, and every other test stays green.
`docker.md` carried `containers/**` until #2127 — a directory this repo never tracks, since
Ansible renders it onto the target host — so the rule reached a session only through its
second glob. The globs are matched the way `inject-nested-docs.py` matches them
(`rule_globs` + `Path.full_match`), so a rule this test accepts is one the injector can fire.

Run: uv run pytest .claude/hooks/tests/test_rules_globs_match_a_tracked_file.py
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import PurePosixPath

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # inject-nested-docs.py imports _hook_common


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(_HERE, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load("inject-nested-docs")
RULES_DIR = os.path.join(os.path.dirname(_HERE), "rules")

# The rules this tree carries. A census that globs for its own subject must know what it
# expects to find, or a renamed directory leaves it green over an empty set.
KNOWN_RULES = frozenset(
    {
        "ansible.md",
        "docker.md",
        "facts.md",
        "generated-docs.md",
        "k8s-templates.md",
        "python-layout.md",
        "secrets.md",
    }
)


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout
    return [PurePosixPath(rel) for rel in out.split("\0") if rel]


def matching_globs(globs, tracked):
    """The subset of `globs` that `full_match` at least one tracked path."""
    return [g for g in globs if any(path.full_match(g) for path in tracked)]


def test_a_glob_over_a_tracked_tree_matches():
    tracked = [PurePosixPath("ansible/roles/k8s/traefik/templates/deployment.yaml.j2")]
    assert matching_globs(["ansible/roles/k8s/**/templates/**"], tracked) == [
        "ansible/roles/k8s/**/templates/**"
    ]


def test_a_glob_over_an_untracked_tree_is_flagged():
    tracked = [
        PurePosixPath(
            "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"
        )
    ]
    assert matching_globs(["containers/**"], tracked) == []


def _rules():
    return sorted(name for name in os.listdir(RULES_DIR) if name.endswith(".md"))


def test_the_census_finds_the_rules_this_tree_carries():
    assert KNOWN_RULES <= set(_rules()), (
        f"missing: {sorted(KNOWN_RULES - set(_rules()))}"
    )


@pytest.mark.parametrize("rule", _rules())
def test_every_rule_glob_matches_a_tracked_file(rule):
    with open(os.path.join(RULES_DIR, rule), encoding="utf-8") as fh:
        globs = _mod.rule_globs(fh.read())
    assert globs, f".claude/rules/{rule} declares no paths: globs, so it never loads"
    tracked = _tracked_files()
    dead = [g for g in globs if g not in matching_globs(globs, tracked)]
    assert not dead, (
        f".claude/rules/{rule} has paths: globs matching no tracked file: {dead} — "
        "the rule never loads for them; fix the glob or drop it"
    )
