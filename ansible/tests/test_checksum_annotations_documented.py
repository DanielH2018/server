"""Every `checksum/<name>` a role's CLAUDE.md names must exist in that role's templates.

Twice now a doc has promised an annotation the manifests do not have: the repo-root CLAUDE.md
pointed at a `checksum/config` that exists nowhere, citing monitor-bridge (whose annotation is
`checksum/check-script`), and autofix-bridge's CLAUDE.md claimed `checksum/config` where the
manifest has `checksum/autofix-script`. Both send someone debugging a pod that will not restart
to look for a mechanism that was never there.

Two instances make it a class, so it becomes a check rather than a third ledger entry.
"""

import re
from pathlib import Path

import pytest
from _helpers import REPO


K8S_ROLES = REPO / "ansible" / "roles" / "k8s"

# `checksum/foo` in prose, including inside backticks. The trailing char class stops at the
# closing backtick or punctuation rather than swallowing the rest of the sentence.
CHECKSUM_RE = re.compile(r"checksum/([a-zA-Z0-9._-]+)")


def _role_dirs():
    return sorted(
        d for d in K8S_ROLES.iterdir() if d.is_dir() and (d / "CLAUDE.md").is_file()
    )


def _annotations_in_templates(role: Path) -> set[str]:
    found = set()
    for path in (
        list(role.rglob("*.j2"))
        + list(role.rglob("*.yaml"))
        + list(role.rglob("*.yml"))
    ):
        if path.name == "CLAUDE.md":
            continue
        found |= set(CHECKSUM_RE.findall(path.read_text(errors="replace")))
    return found


@pytest.mark.parametrize("role", _role_dirs(), ids=lambda p: p.name)
def test_claude_md_checksum_names_exist_in_the_role(role: Path):
    documented = set(
        CHECKSUM_RE.findall((role / "CLAUDE.md").read_text(errors="replace"))
    )
    if not documented:
        pytest.skip("role's CLAUDE.md names no checksum annotation")
    actual = _annotations_in_templates(role)
    missing = documented - actual
    assert not missing, (
        "%s/CLAUDE.md names checksum annotation(s) %s that appear in no template in that role; "
        "the role actually defines %s"
        % (role.name, sorted(missing), sorted(actual) or "none")
    )


def test_root_claude_md_checksum_names_exist_somewhere():
    """The repo-root CLAUDE.md is not scoped to one role, so its names are checked against the
    whole k8s tree. This is the exact claim that was wrong: `checksum/config` existed in no
    template at all while the root doc told you to add one."""
    documented = set(
        CHECKSUM_RE.findall((REPO / "CLAUDE.md").read_text(errors="replace"))
    )
    if not documented:
        pytest.skip("root CLAUDE.md names no checksum annotation")
    actual = set()
    for role in K8S_ROLES.iterdir():
        if role.is_dir():
            actual |= _annotations_in_templates(role)
    missing = documented - actual
    assert not missing, (
        "repo-root CLAUDE.md names checksum annotation(s) %s that exist in no k8s role template"
        % sorted(missing)
    )
