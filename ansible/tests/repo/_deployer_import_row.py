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
    },
)
