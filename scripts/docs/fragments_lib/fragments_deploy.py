"""Doc fragments for the deploy, landing and issue-claiming pages.

Each entry in `FRAGMENTS` builds one fragment the way `gen_doc_fragments.py` builds its own:
a reader that parses the tree statically, and a pure renderer from what it returned to
markdown. `gen_doc_fragments.FRAGMENTS` merges this table, prepends the provenance header and
writes the file, so a builder returns only `(body, sources)`.

The readers live here beside their renderers because no other module shares them. Nothing
imports the code a reader describes: the land verdict enums, the findings CLI and the tree-lock
holders are read with `ast` and `re`, since importing them would bootstrap `sys.path` and read
the environment. The one import is `lib.exit_codes`, a module of constants and a dataclass that
`docs/reference/scripts.md` already renders its own exit-code tables from.
"""

import ast
import re
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.docs_provenance import md_cell
from lib.exit_codes import contract
from lib.repo_paths import REPO

LAND_ENTRY_POINT = "scripts/deploy_tools/land.sh"
LAND_OUTCOME = "scripts/deploy_tools/land_lib/outcome.py"
EXIT_CODES = "scripts/lib/exit_codes.py"

INITIAL_SETUP_TEMPLATES = "ansible/roles/setup/initial_setup/templates"
SETUP_ROLES = "ansible/roles/setup"
DEPLOY_UNDER_LOCKS = "scripts/deploy_tools/deploy_under_locks.py"

ISSUE_MODEL = "scripts/dev/findings_lib/issue_model.py"
FINDINGS_LIB = "scripts/dev/findings_lib"

_LOCK_IMPORT = re.compile(
    r"\{%-?\s*from\s+'git-tree-lock\.j2'\s+import\s+take_git_tree_lock"
)
_LOCK_CALL = re.compile(r"take_git_tree_lock\(\s*['\"]([^'\"]+)['\"]")
_UNIT_LOCK = re.compile(
    r"^ExecStart=.*\bflock\b.*\{\{\s*server_git_tree_lock\s*\}\}", re.M
)


def _code_list(items: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


# --- land.sh verdicts and exit codes ----------------------------------------------------


def enum_values(path: _Path, class_name: str) -> list[str]:
    """The string values of a class's `NAME = "value"` members, in declaration order.

    Args:
        path: the module holding the class.
        class_name: the `StrEnum` to read.

    Raises:
        KeyError: when the module has no such class, or the class has no string members.
    """
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values = [
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ]
            if values:
                return values
    raise KeyError(f"{path.name}: no class `{class_name}` with string members")


def land_exit_codes() -> list[tuple[int, str, str]]:
    """`(value, name, meaning)` for each exit code `land.sh` declares, in code order."""
    codes = contract(LAND_ENTRY_POINT)
    if not codes:
        raise KeyError(f"{EXIT_CODES}: no contract for {LAND_ENTRY_POINT}")
    return [(c.value, c.const, f"{c.meaning} {c.remedy}".strip()) for c in codes]


def render_land_verdicts(
    codes: list[tuple[int, str, str]], verdicts: list[str], causes: list[str]
) -> str:
    """Renders the `land.sh` exit-code table and the two closed vocabularies it prints.

    Args:
        codes: `(value, name, meaning)` for each exit code.
        verdicts: the words the `VERDICT:` line can end with.
        causes: the reasons a `deploy-failed` verdict carries in the landing annotation.
    """
    lines = ["| Exit | Name | Meaning |", "|---|---|---|"]
    for value, name, meaning in codes:
        lines.append(f"| {value} | `{name}` | {md_cell(meaning)} |")
    lines += [
        "",
        f"The `VERDICT:` line ends with one of {len(verdicts)} words: {_code_list(verdicts)}.",
        "",
        f"A `deploy-failed` verdict carries one of {len(causes)} causes in the landing "
        f"annotation: {_code_list(causes)}.",
    ]
    return "\n".join(lines) + "\n"


def _land_verdicts() -> tuple[str, list[str]]:
    outcome = REPO / LAND_OUTCOME
    return render_land_verdicts(
        land_exit_codes(),
        enum_values(outcome, "Verdict"),
        enum_values(outcome, "Cause"),
    ), [EXIT_CODES, LAND_OUTCOME]


# --- who holds the git-tree lock --------------------------------------------------------


def tree_lock_holders(repo: _Path = REPO) -> list[dict[str, str]]:
    """Every job that takes the git-tree lock, derived from the templates that do.

    A holder is a systemd unit whose `ExecStart` wraps `flock` around `server_git_tree_lock`, a
    cron script that imports the `take_git_tree_lock` macro from `git-tree-lock.j2`, or
    `deploy.sh` through `deploy_under_locks.py`. The unit and the cron scripts are found by
    pattern, so a new one joins the list without an edit here.

    Args:
        repo: the tree root, a parameter so a test can hand in a fixture tree.

    Returns:
        One dict per holder with `job`, `how` and `source` (a path relative to `repo`), units
        first, then crons by name, then `deploy.sh`.

    Raises:
        ValueError: when a template imports the macro and never calls it with a job name, or
            when `deploy_under_locks.py` no longer reads the lock path.
    """
    holders = []
    for unit in sorted((repo / SETUP_ROLES).glob("*/templates/*.service.j2")):
        if _UNIT_LOCK.search(unit.read_text()):
            holders.append(
                {
                    "job": unit.name.removesuffix(".j2"),
                    "how": "`flock` around its whole `ExecStart`",
                    "source": unit.relative_to(repo).as_posix(),
                }
            )
    for template in sorted((repo / INITIAL_SETUP_TEMPLATES).glob("*.j2")):
        text = template.read_text()
        if not _LOCK_IMPORT.search(text):
            continue
        jobs = _LOCK_CALL.findall(text)
        if not jobs:
            raise ValueError(
                f"{template.name} imports take_git_tree_lock and never calls it"
            )
        for job in jobs:
            holders.append(
                {
                    "job": job,
                    "how": "`take_git_tree_lock`, held until the script exits",
                    "source": template.relative_to(repo).as_posix(),
                }
            )
    wrapper = repo / DEPLOY_UNDER_LOCKS
    if "tree_lock_path()" not in wrapper.read_text():
        raise ValueError(f"{DEPLOY_UNDER_LOCKS} no longer takes the tree lock")
    holders.append(
        {
            "job": "deploy.sh",
            "how": "`deploy_under_locks.py`, held only while it snapshots `HEAD`",
            "source": DEPLOY_UNDER_LOCKS,
        }
    )
    return holders


def render_tree_lock_holders(holders: list[dict[str, str]]) -> str:
    """Renders the table of jobs that take `/var/lock/server-git-tree.lock`.

    Args:
        holders: dicts with `job`, `how` and `source`, as `tree_lock_holders` returns them.
    """
    lines = ["| Holder | How it takes the lock | Source |", "|---|---|---|"]
    for h in holders:
        lines.append(f"| `{h['job']}` | {h['how']} | `{h['source']}` |")
    return "\n".join(lines) + "\n"


def _tree_lock_holders() -> tuple[str, list[str]]:
    holders = tree_lock_holders()
    return render_tree_lock_holders(holders), sorted({h["source"] for h in holders})


# --- the findings CLI -------------------------------------------------------------------


def _text(node: ast.expr, env: dict[str, str]) -> str:
    """A string constant or an f-string over names in `env`, evaluated without `eval`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue) and isinstance(
                piece.value, ast.Name
            ):
                parts.append(env[piece.value.id])
            else:
                raise ValueError(f"unsupported f-string part: {ast.dump(piece)}")
        return "".join(parts)
    raise ValueError(f"not a string literal: {ast.dump(node)}")


def findings_labels(path: _Path) -> list[tuple[str, str, str]]:
    """`(name, colour, description)` for every label `findings.py sync-labels` creates.

    The literal in `LABELS` gives most of them. The module then adds one `domain/<name>` label
    per entry of `DOMAINS` in a loop, which is read here as the loop's own subscript and tuple.

    Raises:
        KeyError: when `LABELS` or the domain loop is missing.
    """
    tree = ast.parse(path.read_text())
    labels: list[tuple[str, str, str]] = []
    domains: list[str] = []
    loop: ast.For | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "LABELS"
        ):
            assert isinstance(node.value, ast.Dict)
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                assert key is not None, "LABELS holds a ** unpack"
                colour, description = ast.literal_eval(value)
                labels.append((ast.literal_eval(key), colour, description))
        elif isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "DOMAINS" for t in node.targets
        ):
            domains = list(ast.literal_eval(node.value))
        elif isinstance(node, ast.For) and getattr(node.iter, "id", "") == "DOMAINS":
            loop = node
    if not labels:
        raise KeyError(f"{path.name}: no `LABELS` dict")
    if loop is None or not domains:
        raise KeyError(
            f"{path.name}: no `for _d in DOMAINS` loop adding `domain/` labels"
        )
    assign = loop.body[0]
    assert isinstance(assign, ast.Assign) and isinstance(assign.value, ast.Tuple)
    name_node = assign.targets[0].slice  # ty: ignore[unresolved-attribute]
    colour_node, description_node = assign.value.elts
    var = loop.target.id  # ty: ignore[unresolved-attribute]
    for domain in domains:
        env = {var: domain}
        labels.append(
            (
                _text(name_node, env),
                _text(colour_node, env),
                _text(description_node, env),
            )
        )
    return labels


def render_findings_labels(labels: list[tuple[str, str, str]]) -> str:
    """Renders the label table.

    Args:
        labels: `(name, colour, description)` rows.
    """
    lines = ["| Label | Colour | Meaning |", "|---|---|---|"]
    for name, colour, description in labels:
        lines.append(f"| `{name}` | `#{colour}` | {md_cell(description)} |")
    return "\n".join(lines) + "\n"


def findings_subcommands(lib_dir: _Path) -> list[tuple[str, str]]:
    """`(name, help)` for every `add_parser` call in the findings package, in file order.

    The parsers are registered in `cli.py` today. The scan covers the whole package so a
    subcommand that moves to another module stays in the list.
    """
    found = []
    for path in sorted(lib_dir.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser"
            ):
                name = ast.literal_eval(node.args[0])
                help_text = next(
                    (_text(k.value, {}) for k in node.keywords if k.arg == "help"), ""
                )
                found.append((node.lineno, path.name, name, help_text))
    found.sort(key=lambda row: (row[1], row[0]))
    return [(name, help_text) for _, _, name, help_text in found]


def render_findings_subcommands(rows: list[tuple[str, str]]) -> str:
    """Renders the subcommand table.

    Args:
        rows: `(name, help)` for each subcommand.
    """
    lines = ["| Subcommand | What it does |", "|---|---|"]
    for name, help_text in rows:
        lines.append(f"| `{name}` | {md_cell(help_text)} |")
    return "\n".join(lines) + "\n"


def _findings_labels() -> tuple[str, list[str]]:
    return render_findings_labels(findings_labels(REPO / ISSUE_MODEL)), [ISSUE_MODEL]


def _findings_subcommands() -> tuple[str, list[str]]:
    rows = findings_subcommands(REPO / FINDINGS_LIB)
    return render_findings_subcommands(rows), [f"{FINDINGS_LIB}/*.py"]


# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {
    "land-verdicts": _land_verdicts,
    "tree-lock-holders": _tree_lock_holders,
    "findings-labels": _findings_labels,
    "findings-subcommands": _findings_subcommands,
}
