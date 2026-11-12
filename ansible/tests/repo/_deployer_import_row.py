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
# The repo paths #7962 deletes; the role's `end_role` stub stays until H7.55.
_DEPLOYER_DIRS = (
    "roles/setup/gitops_deploy/files",
    "roles/setup/gitops_deploy/templates",
    "roles/setup/gitops_deploy/tests",
)


def _path_parts(node: ast.expr) -> list[str]:
    """The segments of a `/` join, `?` for each one that is not a string literal."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_parts(node.left) + _path_parts(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return ["?"]


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
    joins: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and node.lineno not in joins
        ):
            joined = "/".join(_path_parts(node))
            if found := [d for d in _DEPLOYER_DIRS if d in joined]:
                joins.add(node.lineno)
                hits += [f"line {node.lineno}: joins {d}" for d in found]
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
                for needle in (*_DEPLOYER_STRINGS, *_DEPLOYER_DIRS)
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
        Subject(
            "g.py",
            'SRC = REPO / "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_io.py"\n',
        ),
        Subject(
            "h.py",
            'FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"\n',
        ),
    ),
    green=(
        Subject("a.py", "from lib.tree_lock import TREE_LOCK\n"),
        Subject("b.py", '"""The deployer kept /var/lib/gitops-deploy/hold_sha."""\n'),
        Subject("c.py", "# gitops_markers once lived here\nimport json\n"),
        Subject("d.py", 'contexts = defaults["ci_required_contexts"]\n'),
        Subject(
            "e.py", 'STUB = ROLES / "setup" / "gitops_deploy" / "tasks" / "main.yml"\n'
        ),
    ),
    min_matches=1000,
    must_find=frozenset({"scripts/deploy_tools/await_ci.py"}),
    allow={
        SELF: "the red fixtures above hold the offending names as text",
        **{
            rel: (
                "names the ACL grant on `/var/lib/gitops-deploy`, which stays until the "
                "operator removes the directory"
            )
            for rel in (
                "ansible/tests/k8s/_config_property_rows.py",
                "ansible/tests/setup/test_agent_gitops_read.py",
            )
        },
        **{
            rel: (
                "reads or names a file under the deployer's `files/`, `templates/` or "
                "`tests/`; #7962 deletes the read or repoints the name with that code"
            )
            for rel in (
                "ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py",
                "hosts/components/deploy-ui/tests/test_deploy_ui_app.py",
                "ansible/tests/deploy/test_gitops_deploy_imports.py",
                "ansible/tests/repo/test_census_rows_layout.py",
                "ansible/tests/repo/test_census_rows_python.py",
                "ansible/tests/repo/test_documented_paths_exist.py",
                "ansible/tests/repo/test_entry_points_outside_scripts_answer_help.py",
                "ansible/tests/repo/test_installed_units_log_level.py",
                "ansible/tests/repo/test_module_length_ratchet.py",
                "ansible/tests/repo/test_owner_entry_points_census.py",
                "ansible/tests/repo/test_static_ratchet_hook.py",
                "hosts/lib/tests/test_box_dev_tooling.py",
                "scripts/deploy_tools/tests/test_deploy_tags_blockers.py",
                "scripts/deploy_tools/tests/test_land_policy_import_shapes.py",
                "scripts/deploy_tools/tests/test_land_policy.py",
                "scripts/deploy_tools/tests/test_land_reach.py",
                "scripts/deploy_tools/tests/test_land_tags.py",
                "scripts/dev/tests/test_fanout_autoland.py",
                "scripts/docs/gen_doc_fragments.py",
                "scripts/docs/tests/test_gen_reference_decisions.py",
                "scripts/lib/exit_codes.py",
                "scripts/lib/tests/test_doc_freshness.py",
                "scripts/lib/tests/test_facts_citations.py",
                "scripts/validate/tests/test_validate_setup_templates.py",
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
