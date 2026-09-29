"""No guard censuses `roles/k8s/` with a bare `iterdir()`; they all go through `role_dirs`.

A retired role's gitignored `__pycache__/` keeps `roles/k8s/<role>/` on disk after the
deployer's fast-forward removes its tracked files (#2882). Sixteen guards then read that shell
as a role. Two of them failed outright on a planted shell — the ones that go on to read
`defaults/main.yml` or a role's `CLAUDE.md` raise — and the rest censused or skipped a role
that no longer exists in git (#2952). CI reads a fresh checkout, so neither half showed up
there — the failure lands only in the long-lived primary checkout, which is where
the prek pre-push `pytest` hook runs.

`_role_census.role_dirs` is the one reader, filtering with the deployer's own
`k8s_autodeploy.is_leftover_dir` predicate. This guard is what keeps the next walk from being
written bare, because the bare form passes every test on a fresh checkout.

The scan is receiver-name-based: it sees `K8S_ROLES.iterdir()` and
`(REPO / "ansible/roles/k8s").iterdir()`, and it cannot see a walk that reaches the directory
through a name that mentions neither (`_ROLE.parent.iterdir()`, the shape
`test_cronjob_gate_decision.py` used). `EXPECTED_ROLE_DIRS_CALLERS` below covers that half:
those files must keep calling `role_dirs`, whatever spelling a rewrite reaches for.

The scan covers tracked `.py` files under `ansible/tests` only. The `roles/k8s/` walkers
outside the suite — `scripts/validate/k8s_manifests.py`, `scripts/docs/catalog_backup.py`,
`scripts/dev/k8s_autodeploy_counts.py`, `scripts/diagnostics/probe_lib/releases_retired.py` —
were fixed by #2882 and #2888 and call `is_leftover_dir` directly. They are deliberately out
of scope here: they read the tree through `validate.k8s_manifests`, not through the suite's
`_role_census`, so a guard over them would have a different one-true-reader to name.

Run: uv run pytest ansible/tests/repo/test_k8s_role_walks_use_role_dirs.py
"""

import re
import subprocess

from _helpers import ANSIBLE, REPO

# `<anything naming the k8s roles dir>.iterdir()` on one line. Anchored on the receiver rather
# than on `iterdir` alone, because walks of `roles/setup/` and the Pi's `roles/containers/` are
# a different population — a retired role there ghosts too, but no k8s guard reads them.
# Three spellings reach the directory: the `K8S_ROLES` constant every guard imports, the
# literal path `_helpers.py` and `test_restart_on_narrows_the_restart_signals.py` both used,
# and `ROLES / "k8s"`, which is how `_helpers.py` builds the constant in the first place.
BARE_K8S_WALK = re.compile(
    r'(?:K8S_ROLES|roles/k8s"|ROLES\s*/\s*"k8s")[^\n]*\.iterdir\(\)'
)

# Every module whose k8s role census goes through `role_dirs`. Pinned by name so a rewrite
# that drops the call fails here even when it also drops the spelling the regex reads.
EXPECTED_ROLE_DIRS_CALLERS = frozenset(
    {
        "_k8s_render.py",
        "k8s/test_checksum_annotations_documented.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "k8s/test_k8s_roles_have_claude_md.py",
        "k8s/test_manifest_roles_include_the_shared_render.py",
        "k8s/test_vip_pins.py",
        "longhorn/test_every_longhorn_pvc_has_a_tier.py",
        "deploy/test_cronjob_gate_decision.py",
        "deploy/test_cronjob_only_roles_include_the_gate.py",
        "deploy/test_inline_rollout_gates.py",
        "deploy/test_k8s_autodeploy_guard.py",
        "deploy/test_k8s_dry_run.py",
        "deploy/test_k8s_dry_run_host_writes.py",
        "deploy/test_manifests_prune.py",
        "deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py",
        "deploy/test_restart_on_narrows_the_restart_signals.py",
    }
)

# The modules that may still walk the tree bare: `_role_census.role_dirs` is the census itself,
# and the three guards below call `is_leftover_dir` inline against the same predicate (their
# comments argue it at the line).
ALLOWED_BARE = {
    "_role_census.py",
    "deploy/_autodeploy.py",
    "deploy/test_denylist_parsers_agree.py",
    "deploy/test_k8s_autodeploy_denylist.py",
}

SELF = "repo/test_k8s_role_walks_use_role_dirs.py"


def _tracked_test_modules() -> list[str]:
    """Every tracked `.py` under ansible/tests, relative to it."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=ANSIBLE / "tests",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def test_the_scan_finds_the_guards_it_is_about():
    """Without this the two tests below pass on an empty file list."""
    modules = _tracked_test_modules()
    assert len(modules) >= 100
    assert EXPECTED_ROLE_DIRS_CALLERS <= set(modules)


def test_no_guard_censuses_the_k8s_roles_with_a_bare_iterdir():
    offenders = []
    for rel in _tracked_test_modules():
        if rel in ALLOWED_BARE or rel == SELF:
            continue
        text = (ANSIBLE / "tests" / rel).read_text(errors="replace")
        if BARE_K8S_WALK.search(text):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} walk roles/k8s/ with a bare .iterdir(). A retired role's leftover "
        f"__pycache__/ shell reads as a role there, which fails or miscensuses only in a "
        f"long-lived checkout (#2952). Use `_helpers.role_dirs(...)`."
    )


def test_every_rewritten_guard_still_calls_role_dirs():
    missing = sorted(
        rel
        for rel in EXPECTED_ROLE_DIRS_CALLERS
        if "role_dirs(" not in (ANSIBLE / "tests" / rel).read_text(errors="replace")
    )
    assert not missing, (
        f"{missing} no longer call role_dirs(). Their k8s role census was routed through it "
        f"so a retired role's __pycache__ shell cannot read as a role (#2952); if one of them "
        f"genuinely stopped censusing roles, drop it from EXPECTED_ROLE_DIRS_CALLERS with the "
        f"reason."
    )


def test_the_pattern_rejects_a_bare_walk_and_accepts_a_filtered_one():
    """Red-proof pair for BARE_K8S_WALK itself."""
    assert BARE_K8S_WALK.search("for p in sorted(_K8S_ROLES.iterdir()):")
    assert BARE_K8S_WALK.search('for p in (REPO / "ansible/roles/k8s").iterdir():')
    assert BARE_K8S_WALK.search('for p in (ROLES / "k8s").iterdir():')
    assert not BARE_K8S_WALK.search("for p in role_dirs():")
    assert not BARE_K8S_WALK.search("for p in role_dirs(K8S_ROLES):")
    # A setup-plane or Pi walk is out of scope, not an offender.
    assert not BARE_K8S_WALK.search("for p in _SETUP_ROLES_DIR.iterdir():")


def test_the_repo_root_is_the_one_this_guard_reads():
    assert (REPO / "ansible" / "tests" / "_helpers.py").is_file()
