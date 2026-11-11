"""The deployer-import census row: nothing outside the deployer's role reaches the deployer.

H7.53 (`docs/target-architecture/host/h7-delete-ansible.md`) deletes the GitOps deployer. The
row sits in its own module because `test_census_rows_python.py`, which imports it into `ROWS`,
is at the cap for a test module.
"""

import ast

from _row_table import Census, Subject, tracked

SELF = "ansible/tests/repo/_deployer_import_row.py"


def _parse(subject: Subject) -> ast.AST | None:
    """The module's AST, or None for a file this interpreter cannot parse."""
    try:
        return ast.parse(subject.text)
    except SyntaxError:
        return None


_DEPLOYER_MODULES = frozenset(
    {"deployer_lib", "gitops_hold", "gitops_markers", "gitops_ledger"}
)
_DEPLOYER_NAMES = frozenset({"GITOPS_DEPLOY_FILES"})
_DEPLOYER_STRINGS = (
    "/etc/gitops-deploy",
    "/var/lib/gitops-deploy",
    "/opt/gitops-deploy",
    "gitops_tick.sh",
    "gitops_deploy_ci_contexts",
    "gitops_deploy_ci_ancestor_walk_max",
)


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of every module, class and function docstring node, which are prose."""
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _deployer_offence(subject: Subject) -> list[str]:
    tree = _parse(subject)
    if tree is None:
        return []
    prose = _docstrings(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                modules += [
                    n for n in names if n in _DEPLOYER_MODULES | _DEPLOYER_NAMES
                ]
            else:
                modules = names
            hits += [
                f"line {node.lineno}: imports {module}"
                for module in modules
                if (_DEPLOYER_MODULES | _DEPLOYER_NAMES) & set(module.split("."))
            ]
        if (isinstance(node, ast.Name) and node.id in _DEPLOYER_NAMES) or (
            isinstance(node, ast.Attribute) and node.attr in _DEPLOYER_NAMES
        ):
            hits.append(f"line {node.lineno}: uses GITOPS_DEPLOY_FILES")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
        ):
            hits += [
                f"line {node.lineno}: names {needle}"
                for needle in _DEPLOYER_STRINGS
                if needle in node.value
            ]
    return hits


DEPLOYER_IMPORTS = Census(
    name="nothing-reaches-the-deleted-deployer",
    reason=(
        "H7.53 deletes the GitOps deployer's code, its state readers and its paths. An "
        "import of `deployer_lib`, `gitops_hold`, `gitops_markers` or `gitops_ledger`, a "
        "use of `GITOPS_DEPLOY_FILES`, or a string naming the deployer's directories, its "
        "tick wrapper or its two CI config keys would bring back a reader of state no host "
        "writes. The walk reads every import, function-level ones too, and every string "
        "but a docstring, without importing anything. `DEPLOY_HOLD` replaced the hold."
    ),
    files=lambda: [
        rel
        for rel in tracked("*.py")
        if not rel.startswith(
            ("ansible/collections/", "ansible/roles/setup/gitops_deploy/")
        )
    ],
    offence=_deployer_offence,
    red=(
        Subject("a.py", "from deployer_lib.deploy_locks import TREE_LOCK\n"),
        Subject("b.py", "def f():\n    import gitops_markers\n"),
        Subject("c.py", 'HOLD = "/var/lib/gitops-deploy/hold_sha"\n'),
        Subject("d.py", 'contexts = defaults["gitops_deploy_ci_contexts"]\n'),
        Subject("e.py", "from lib.repo_paths import GITOPS_DEPLOY_FILES\n"),
        Subject("f.py", "from lib import repo_paths\nrepo_paths.GITOPS_DEPLOY_FILES\n"),
    ),
    green=(
        Subject("a.py", "from lib.tree_lock import TREE_LOCK\n"),
        Subject("b.py", '"""The deployer kept /var/lib/gitops-deploy/hold_sha."""\n'),
        Subject("c.py", "# gitops_markers once lived here\nimport json\n"),
        Subject("d.py", 'contexts = defaults["ci_required_contexts"]\n'),
    ),
    min_matches=1000,
    must_find=frozenset({"scripts/deploy_tools/await_ci.py"}),
    allow={
        SELF: "the red fixtures above hold the offending names as text",
        **{
            rel: (
                "reads the deployer's hold, markers or ledger, which no host writes "
                "since H7.52; the read goes or switches to DEPLOY_HOLD in #7961"
            )
            for rel in (
                "ansible/roles/k8s/monitor-bridge/files/gitops_hold.py",
                "ansible/roles/k8s/monitor-bridge/files/gitops_ledger.py",
                "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",
                "ansible/tests/deploy/test_gitops_markers_copies.py",
                "ansible/tests/k8s/_config_property_rows.py",
                "ansible/tests/setup/test_agent_gitops_read.py",
                "hosts/components/deploy-ui/files/deploy_ui.py",
                "hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_reads.py",
                "hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_writes.py",
                "hosts/components/deploy-ui/tests/test_deploy_ui_app.py",
                "hosts/components/deploy-ui/tests/test_deploy_ui_reads.py",
                "hosts/components/renovate-agent/files/agent_logic.py",
                "hosts/components/renovate-agent/files/agent_toolbox.py",
                "hosts/components/renovate-agent/files/renovate_agent.py",
                "hosts/components/renovate-agent/tests/test_agent_logic.py",
                "hosts/components/renovate-agent/tests/test_session_resume.py",
                "scripts/deploy_tools/gitops_state.py",
                "scripts/deploy_tools/owed_run.py",
                "scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py",
                "scripts/deploy_tools/runbook_gates_lib/metallb_upgrade.py",
                "scripts/deploy_tools/tests/test_gitops_state.py",
                "scripts/deploy_tools/tests/test_gitops_state_hold.py",
                "scripts/deploy_tools/tests/test_gitops_state_owed.py",
                "scripts/deploy_tools/tests/test_k3s_upgrade_held_sha_unreadable.py",
                "scripts/deploy_tools/tests/test_runbook_gates_k3s_upgrade.py",
                "scripts/deploy_tools/tests/test_runbook_gates_metallb_upgrade.py",
                "scripts/dev/fanout_lib/operator_waits.py",
                "scripts/diagnostics/probe_lib/gitops_view.py",
                "scripts/diagnostics/probe_lib/gitops_why.py",
                "scripts/diagnostics/probe_lib/landing_blockers.py",
                "scripts/diagnostics/tests/test_probe_gitops_state.py",
                "scripts/diagnostics/tests/test_ui_smoke.py",
                "scripts/docs/reference/state.py",
                "scripts/lib/deployer_park.py",
                "scripts/lib/tests/test_deployer_park.py",
            )
        },
        **{
            rel: (
                "deploy.sh, setup narrowing, land.sh's setup and Pi arms, or a test of "
                "the deployer's own code; deleted with its subject in #7962"
            )
            for rel in (
                "ansible/roles/setup/homelab_builder/tests/test_builder_ci_grading.py",
                "ansible/tests/deploy/test_deploy_sh_takes_the_locks_deploy_locks_plans.py",
                "ansible/tests/deploy/test_k3s_pin_routing.py",
                "ansible/tests/deploy/test_volume_revert_playbook_applies_nothing_from_a_tick.py",
                "ansible/tests/repo/test_ci_contexts_match_workflows.py",
                "ansible/tests/setup/test_initial_setup_roles_are_visible_to_the_deployer.py",
                "ansible/tests/setup/test_setup_crons_write_a_textfile.py",
                "ansible/tests/setup/test_setup_cross_role_files.py",
                "ansible/tests/setup/test_setup_cross_role_reads.py",
                "hosts/components/renovate-notify/tests/test_renovate_notify.py",
                "scripts/deploy_tools/deploy_lib/detach_notify.py",
                "scripts/deploy_tools/deploy_lib/playbook.py",
                "scripts/deploy_tools/deploy_lib/run.py",
                "scripts/deploy_tools/deploy_lib/tags.py",
                "scripts/deploy_tools/deploy_lib/under_locks.py",
                "scripts/deploy_tools/land_lib/classify.py",
                "scripts/deploy_tools/land_lib/land_reach.py",
                "scripts/deploy_tools/land_lib/land_shared.py",
                "scripts/deploy_tools/land_lib/land_tags.py",
                "scripts/deploy_tools/land_lib/options.py",
                "scripts/deploy_tools/land_lib/outcome.py",
                "scripts/deploy_tools/land_lib/tools.py",
                "scripts/deploy_tools/narrow_lib/paths.py",
                "scripts/deploy_tools/narrow_lib/role_reads.py",
                "scripts/deploy_tools/narrow_lib/secret_keys.py",
                "scripts/deploy_tools/narrow_setup.py",
                "scripts/deploy_tools/reach.py",
                "scripts/deploy_tools/setup_routing.py",
                "scripts/deploy_tools/tests/_deploy_sh_fakes.py",
                "scripts/deploy_tools/tests/test_deploy_detach_notify.py",
                "scripts/deploy_tools/tests/test_deploy_staleness.py",
                "scripts/deploy_tools/tests/test_deploy_staleness_isolation.py",
                "scripts/deploy_tools/tests/test_forge_check_run_pages.py",
                "scripts/deploy_tools/tests/test_land_classify.py",
                "scripts/deploy_tools/tests/test_land_reach_repo_shipped_files.py",
                "scripts/deploy_tools/tests/test_land_reach_vars_files.py",
                "scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py",
                "scripts/deploy_tools/tests/test_land_tags_shared_mapper_agreement.py",
                "scripts/deploy_tools/tests/test_narrow_secret_keys.py",
                "scripts/deploy_tools/tests/test_narrow_setup.py",
                "scripts/deploy_tools/tests/test_narrow_setup_exit_contract.py",
                "scripts/deploy_tools/tests/test_narrow_setup_vars_walk.py",
                "scripts/deploy_tools/tests/test_promote_ci_contexts.py",
                "scripts/deploy_tools/tests/test_setup_reach_hosts.py",
                "scripts/deploy_tools/tests/test_setup_readers_outlive_deploy_narrowing.py",
                "scripts/deploy_tools/tests/test_setup_routing.py",
                "scripts/deploy_tools/tests/test_wrapper_lock_wait_lines.py",
                "scripts/lib/repo_paths.py",
                "scripts/lib/tests/test_stdlib_only.py",
                "scripts/lib/tests/test_tree_lock.py",
            )
        },
    },
)
