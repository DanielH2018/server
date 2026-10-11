"""The landing policy's import-shape rules, driven through `policy.check`.

Each rule refuses a change that can put code into land.py's import closure with no import
line of its own, so a later approved PR would pull in code its diff never shows. A case is a
PR's REST file listing plus the tree at its head, as `{path: mode}`.

`gate_hits` also runs, and it reads the test session's `sys.modules`, which depends on what
else the worker imported. Each refusal therefore asserts the rule's own reason, and each
admitted case changes only paths no test imports: nothing under `src/`, and no `.py` file
under a role's `files/`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_policy_import_shapes.py
"""

import sys

import pytest

import land  # noqa: F401  # inserts the lander's sys.path roots, which the rules read
from _land_fakes import Fakes
from deploy_tools.land_lib import merge, policy
from deploy_tools.land_lib.outcome import Outcome

HEAD = "c" * 40
PREFIX = "worktree-claude+"
APPROVER = "operator-login"
EMPTY = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
CODE = "1" * 40
POLICY_FIELDS = "headRefOid,headRefName,baseRefName,isCrossRepository"
F = "100644"
V = "src/homelab/validate"
BASE_TREE = {f"{V}/validate_lib.py": F, f"{V}/k8s_schema.py": F}


def _arm(landing, tmp_path, files, tree, reviews=(), **opts):
    approval = tmp_path / "approval-paths"
    approval.write_text("ansible/roles/setup/claude_code/\n")
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Harden the lander",
                "body": "",
                "reviewDecision": "REVIEW_REQUIRED",
            },
            POLICY_FIELDS: {
                "headRefOid": HEAD,
                "headRefName": "worktree-claude+shapes",
                "baseRefName": "master",
                "isCrossRepository": False,
            },
            "headRefOid": {"headRefOid": HEAD},
        },
        pr_files=files,
        head_tree=tree,
        pr_reviews=list(reviews),
        **opts.pop("fakes", {}),
    )
    ln, calls = landing(
        fakes,
        arm_merge=True,
        await_merge=True,
        require_branch_prefix=PREFIX,
        approval_paths=str(approval),
        **opts,
    )
    merge.arm_merge(ln)
    return ln, calls


def _added(path, sha=CODE):
    return {"status": "added", "filename": path, "sha": sha}


def _removed(path):
    return {"status": "removed", "filename": path, "sha": CODE}


def _renamed(old, new, sha=CODE):
    return {"status": "renamed", "filename": new, "previous_filename": old, "sha": sha}


# Each case: the listing, the head tree, and the hit the refusal must name.
REFUSED = {
    "an __init__.py deleted for a sibling module": (
        [_removed(f"{V}/__init__.py"), _added("src/homelab/validate.py")],
        {**BASE_TREE, "src/homelab/validate.py": F},
        f"{V}/__init__.py (an __init__.py left a directory that still holds Python files)",
    ),
    "an __init__.py moved away under the same basename, listed renamed": (
        [
            _renamed(f"{V}/__init__.py", "src/homelab/newpkg/__init__.py", EMPTY),
            _added("src/homelab/validate.py"),
        ],
        {
            **BASE_TREE,
            "src/homelab/newpkg/__init__.py": F,
            "src/homelab/validate.py": F,
        },
        f"{V}/__init__.py (an __init__.py left a directory that still holds Python files)",
    ),
    # Whether GitHub pairs an empty-blob move as a rename is unmeasured, so both forms refuse.
    "an __init__.py moved away under the same basename, listed added and removed": (
        [
            _added("src/homelab/newpkg/__init__.py", EMPTY),
            _added("src/homelab/validate.py"),
            _removed(f"{V}/__init__.py"),
        ],
        {
            **BASE_TREE,
            "src/homelab/newpkg/__init__.py": F,
            "src/homelab/validate.py": F,
        },
        f"{V}/__init__.py (an __init__.py left a directory that still holds Python files)",
    ),
    "a package directory replaced by a symlink": (
        [
            _added(V),
            _removed(f"{V}/__init__.py"),
            _added("elsewhere/__init__.py", EMPTY),
        ],
        {V: "120000", "elsewhere/__init__.py": F},
        f"{V} (a symlink)",
    ),
    "a listed path missing from the head tree": (
        [_added("scripts/dev/tool.py")],
        {"scripts/dev/other.py": F},
        "scripts/dev/tool.py (missing from the head tree)",
    ),
    "a module beside a same-named directory, at depth": (
        [_added("scripts/validate/validate_lib.py")],
        {
            "scripts/validate/validate_lib.py": F,
            "scripts/validate/validate_lib/k8s_schema.py": F,
        },
        "scripts/validate/validate_lib.py (a module beside a same-named directory)",
    ),
    "a module renamed in beside a same-named directory": (
        [_renamed("docs/notes.py", "scripts/validate.py")],
        {"scripts/validate.py": F, "scripts/validate/x.py": F},
        "scripts/validate.py (a module beside a same-named directory)",
    ),
    "a module in one import root shadowing a directory in another": (
        [_added("scripts/deploy_tools/validate.py")],
        {"scripts/deploy_tools/validate.py": F, "scripts/validate/x.py": F},
        "scripts/deploy_tools/validate.py (shadows scripts/validate in another import root)",
    ),
    "a module in one import root shadowing a module in another": (
        [_added("scripts/deploy_tools/disk_report.py")],
        {
            "scripts/deploy_tools/disk_report.py": F,
            "ansible/roles/setup/common/files/disk_report.py": F,
        },
        "scripts/deploy_tools/disk_report.py (shadows "
        "ansible/roles/setup/common/files/disk_report.py in another import root)",
    ),
    "a non-empty __init__.py": (
        [_added("scripts/home_assistant/__init__.py")],
        {"scripts/home_assistant/__init__.py": F, "scripts/home_assistant/ha.py": F},
        "scripts/home_assistant/__init__.py (a non-empty __init__.py)",
    ),
    "an __init__.py the listing gives no blob for": (
        [{"status": "modified", "filename": "scripts/home_assistant/__init__.py"}],
        {"scripts/home_assistant/__init__.py": F},
        "scripts/home_assistant/__init__.py (a non-empty __init__.py)",
    ),
    "an added __main__.py": (
        [_added("src/homelab/lib/__main__.py", EMPTY)],
        {"src/homelab/lib/__main__.py": F},
        "src/homelab/lib/__main__.py (a __main__.py)",
    ),
}

ADMITTED = {
    "a whole subpackage deleted": (
        [
            _removed(f"{V}/__init__.py"),
            _removed(f"{V}/validate_lib.py"),
            _removed(f"{V}/k8s_schema.py"),
        ],
        {"src/homelab/__init__.py": F},
    ),
    "a whole package moved": (
        [
            _renamed(f"{V}/__init__.py", "src/homelab/check/__init__.py", EMPTY),
            _renamed(f"{V}/validate_lib.py", "src/homelab/check/validate_lib.py"),
            _renamed(f"{V}/k8s_schema.py", "src/homelab/check/k8s_schema.py"),
        ],
        {
            "src/homelab/check/__init__.py": F,
            "src/homelab/check/validate_lib.py": F,
            "src/homelab/check/k8s_schema.py": F,
        },
    ),
    "a package gaining an empty __init__.py": (
        [
            _added(f"{V}/__init__.py", EMPTY),
            _renamed("src/homelab/legacy/k8s_schema.py", f"{V}/k8s_schema.py"),
        ],
        {f"{V}/__init__.py": F, f"{V}/k8s_schema.py": F},
    ),
    "a non-empty __init__.py emptied": (
        [{"status": "modified", "filename": "src/homelab/__init__.py", "sha": EMPTY}],
        {"src/homelab/__init__.py": F},
    ),
    "a Vale vocabulary file": (
        [
            {
                "status": "modified",
                "filename": "styles/config/vocabularies/Homelab/accept.txt",
                "sha": CODE,
            }
        ],
        {"styles/config/vocabularies/Homelab/accept.txt": F},
    ),
    "a role module named like the project": (
        [
            {
                "status": "modified",
                "filename": "ansible/roles/k8s/homelab-mcp/templates/deployment.yaml.j2",
                "sha": CODE,
            }
        ],
        {"ansible/roles/k8s/homelab-mcp/templates/deployment.yaml.j2": F},
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_an_import_shape_is_flagged(landing, tmp_path, case):
    files, tree, hit = REFUSED[case]
    with pytest.raises(Outcome) as exc:
        _arm(landing, tmp_path, files, tree)
    assert "need the operator's approval" in exc.value.error
    assert hit in exc.value.error


@pytest.mark.parametrize("case", sorted(ADMITTED))
def test_an_ordinary_change_is_clean(landing, tmp_path, case):
    files, tree = ADMITTED[case]
    ln, _ = _arm(landing, tmp_path, files, tree)
    assert ln.pinned_head == HEAD


def test_the_approvers_approval_of_the_head_lifts_a_shape_refusal(landing, tmp_path):
    files, tree, _ = REFUSED["an added __main__.py"]
    approval = {
        "user": {"login": APPROVER},
        "state": "APPROVED",
        "commit_id": HEAD,
        "submitted_at": "2026-10-11T20:00:00Z",
    }
    ln, calls = _arm(landing, tmp_path, files, tree, [approval], approver=APPROVER)
    assert ln.pinned_head == HEAD
    assert [c for c in calls if c[0] == "gh:reviews"]


def test_the_head_tree_is_read_at_the_pinned_head(landing, tmp_path):
    files, tree = ADMITTED["a Vale vocabulary file"]
    _, calls = _arm(landing, tmp_path, files, tree)
    (call,) = [c for c in calls if c[0] == "git" and c[1][0] == "ls-tree"]
    assert call[1][-1] == HEAD


@pytest.mark.parametrize(
    ("tree", "rc"), [({}, 0), ({"docs/x.md": F}, 128)], ids=["empty", "failed"]
)
def test_an_unreadable_head_tree_refuses_even_for_the_approver(
    landing, tmp_path, tree, rc
):
    files, _ = ADMITTED["a Vale vocabulary file"]
    with pytest.raises(Outcome) as exc:
        _arm(
            landing,
            tmp_path,
            files,
            tree,
            approver=APPROVER,
            fakes={"ls_tree_rc": rc},
        )
    assert "could not read the tree at the PR head" in exc.value.error


def test_the_import_roots_are_the_landers_own_inserts():
    roots = policy.import_roots(policy.CHECKOUT, sys.path)
    assert {
        "scripts",
        "scripts/deploy_tools",
        "ansible/roles/setup/gitops_deploy/files",
        "ansible/roles/setup/common/files",
    } <= set(roots)
    assert "" not in roots
