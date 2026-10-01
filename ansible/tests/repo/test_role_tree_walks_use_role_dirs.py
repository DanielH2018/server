"""No guard censuses a role tree with a bare `iterdir()`; they all go through `role_dirs`.

A retired role's gitignored `__pycache__/` keeps `roles/<plane>/<role>/` on disk after the
deployer's fast-forward removes its tracked files. A guard that censuses with a bare
`iterdir()` then reads that shell as a role. A guard that goes on to read `defaults/main.yml`
or a role's `CLAUDE.md` raises, and the rest census or skip a role that no longer exists in
git. CI reads a fresh checkout, so neither failure shows up there — it lands only in the
long-lived primary checkout, which is where the prek pre-push `pytest` hook runs.

All three trees ghost the same way. The Pi's `roles/containers/` and `roles/setup/` fail
LOUDER than k8s does: both `test_containers_list_roles_exist.py` and
`test_setup_role_playbooks_agree.py` assert against a declaration (`containers_list`, the
playbooks), so a shell reads as an undeclared role and the guard fails rather than
miscensusing. This scan covers all three trees.

`_role_census.role_dirs` is the one reader, filtering with the deployer's own
`k8s_autodeploy.is_leftover_dir` predicate. This guard is what keeps the next walk from being
written bare, because the bare form passes every test on a fresh checkout.

The scan is receiver-name-based: it sees `K8S_ROLES.iterdir()` and
`(REPO / "ansible/roles/k8s").iterdir()`, and it cannot see a walk that reaches a tree through
a name that mentions neither (`_ROLE.parent.iterdir()`). `EXPECTED_ROLE_DIRS_CALLERS` below covers that half:
those files must keep calling `role_dirs`, whatever spelling a rewrite reaches for.

Two more such spellings walk `roles/k8s/` unfiltered: a from-import alias
(`from _helpers import K8S_ROLES as K8S`, then `sorted(K8S.iterdir())`) and a path rebuilt
segment by segment (`K8S = REPO / "ansible" / "roles" / "k8s"`). Both are routed through
`role_dirs` and listed below. The segmented literal is in the pattern, since
`"roles" / "k8s"` is as readable as `"roles/k8s"` — but only where the walk is on that same
line. The pattern is one line wide by construction, so a tree bound to a constant on one line
and walked on another is invisible to it whatever the spelling, and the list is what covers
that.

The scan covers tracked `.py` files under `ansible/tests` only. The role-tree walkers
outside the suite — `scripts/validate/k8s_manifests.py`, `scripts/docs/catalog_backup.py`,
`scripts/docs/glance_facts.py`, `scripts/dev/k8s_autodeploy_counts.py`,
`scripts/diagnostics/probe_lib/releases_retired.py` — call `is_leftover_dir` directly. They are deliberately out of scope here: they read the tree
through their own module, not through the suite's `_role_census`, so a guard over them would
have a different one-true-reader to name.

Run: uv run pytest ansible/tests/repo/test_role_tree_walks_use_role_dirs.py
"""

import re

from _helpers import ANSIBLE, REPO
from lib.proc_testing import run

# `<anything naming a role tree>.iterdir()` on one line. Anchored on the receiver rather than
# on `iterdir` alone, so a walk of some other directory is not an offender. Two spellings per
# tree reach it: the constant a guard imports (`K8S_ROLES`, `SETUP_ROLES`, `CONTAINER_ROLES`,
# and the local aliases that end in one of those names — `_SETUP_ROLES_DIR`, `DOCKER_ROLES`),
# and the literal path, written out or built a segment at a time (`ROLES / "<plane>"`,
# `REPO / "ansible" / "roles" / "<plane>"`).
BARE_ROLE_WALK = re.compile(
    r"(?:K8S_ROLES|SETUP_ROLES|CONTAINER_ROLES|CONTAINERS_ROLES|DOCKER_ROLES"
    r'|roles/(?:k8s|setup|containers)"'
    r'|ROLES\s*/\s*"(?:k8s|setup|containers)"'
    r'|"roles"\s*/\s*"(?:k8s|setup|containers)")[^\n]*\.iterdir\(\)'
)

# Every module whose role census goes through `role_dirs`. Pinned by name so a rewrite that
# drops the call fails here even when it also drops the spelling the regex reads.
EXPECTED_ROLE_DIRS_CALLERS = frozenset(
    {
        "_k8s_render.py",
        "k8s/test_checksum_annotations_documented.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "k8s/test_k8s_roles_have_claude_md.py",
        "k8s/test_manifest_roles_include_the_shared_render.py",
        "k8s/test_script_configmaps_apply_server_side.py",
        "k8s/test_vip_pins.py",
        "longhorn/test_every_longhorn_pvc_has_a_tier.py",
        "deploy/test_containers_list_roles_exist.py",
        "deploy/test_cronjob_gate_decision.py",
        "deploy/test_cronjob_only_roles_include_the_gate.py",
        "deploy/test_inline_rollout_gates.py",
        "deploy/test_k8s_autodeploy_guard.py",
        "deploy/test_k8s_dry_run.py",
        "deploy/test_k8s_dry_run_host_writes.py",
        "deploy/test_manifests_prune.py",
        "deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py",
        "deploy/test_restart_on_narrows_the_restart_signals.py",
        "deploy/test_setup_role_playbooks_agree.py",
        "services/test_bridge_patch_boundary.py",
        "services/test_container_roles_have_claude_md.py",
        "setup/test_has_flag_roles_have_both_directions.py",
        "setup/test_host_lib_sibling_copies.py",
        "setup/test_setup_roles_have_claude_md.py",
    }
)

# The modules that may still walk a tree bare: `_role_census.role_dirs` is the census itself,
# and the three guards below call `is_leftover_dir` inline against the same predicate (their
# comments argue it at the line).
ALLOWED_BARE = {
    "_role_census.py",
    "deploy/_autodeploy.py",
    "deploy/test_denylist_parsers_agree.py",
    "deploy/test_k8s_autodeploy_denylist.py",
}

SELF = "repo/test_role_tree_walks_use_role_dirs.py"


def _tracked_test_modules() -> list[str]:
    """Every tracked `.py` under ansible/tests, relative to it."""
    listed = run(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=ANSIBLE / "tests", check=True
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def test_the_scan_finds_the_guards_it_is_about():
    """Without this the two tests below pass on an empty file list."""
    modules = _tracked_test_modules()
    assert len(modules) >= 100
    assert EXPECTED_ROLE_DIRS_CALLERS <= set(modules)


def test_no_guard_censuses_a_role_tree_with_a_bare_iterdir():
    offenders = []
    for rel in _tracked_test_modules():
        if rel in ALLOWED_BARE or rel == SELF:
            continue
        text = (ANSIBLE / "tests" / rel).read_text(errors="replace")
        if BARE_ROLE_WALK.search(text):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} walk a roles/ tree with a bare .iterdir(). A retired role's leftover "
        f"__pycache__/ shell reads as a role there, which fails or miscensuses only in a "
        f"long-lived checkout (#2952, #2964). Use `_role_census.role_dirs(...)`."
    )


def test_every_rewritten_guard_still_calls_role_dirs():
    missing = sorted(
        rel
        for rel in EXPECTED_ROLE_DIRS_CALLERS
        if "role_dirs(" not in (ANSIBLE / "tests" / rel).read_text(errors="replace")
    )
    assert not missing, (
        f"{missing} no longer call role_dirs(). Their role census was routed through it "
        f"so a retired role's __pycache__ shell cannot read as a role (#2952, #2964); if one "
        f"of them genuinely stopped censusing roles, drop it from EXPECTED_ROLE_DIRS_CALLERS "
        f"with the reason."
    )


def test_the_pattern_rejects_a_bare_walk_and_accepts_a_filtered_one():
    """Red-proof pair for BARE_ROLE_WALK itself, one positive per tree."""
    assert BARE_ROLE_WALK.search("for p in sorted(_K8S_ROLES.iterdir()):")
    assert BARE_ROLE_WALK.search('for p in (REPO / "ansible/roles/k8s").iterdir():')
    assert BARE_ROLE_WALK.search('for p in (ROLES / "k8s").iterdir():')
    assert BARE_ROLE_WALK.search(
        'for p in (REPO / "ansible" / "roles" / "k8s").iterdir():'
    )
    assert BARE_ROLE_WALK.search("for p in sorted(_SETUP_ROLES_DIR.iterdir()):")
    assert BARE_ROLE_WALK.search('for p in (ROLES / "setup").iterdir():')
    assert BARE_ROLE_WALK.search("for p in DOCKER_ROLES.iterdir():")
    assert BARE_ROLE_WALK.search(
        'for p in (REPO / "ansible/roles/containers").iterdir():'
    )
    assert not BARE_ROLE_WALK.search("for p in role_dirs():")
    assert not BARE_ROLE_WALK.search("for p in role_dirs(K8S_ROLES):")
    assert not BARE_ROLE_WALK.search("for p in role_dirs(SETUP_ROLES):")
    # A walk of some other directory is out of scope, not an offender.
    assert not BARE_ROLE_WALK.search("for p in (role / 'templates').iterdir():")


def test_the_repo_root_is_the_one_this_guard_reads():
    assert (REPO / "ansible" / "tests" / "_helpers.py").is_file()
