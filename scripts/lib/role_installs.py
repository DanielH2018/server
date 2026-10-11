"""Where a host installs each role script, and which of them a host runs `--no-project`.

A role script runs from the path its role's `copy` task installs it to, so its `sys.path` is
the install directory, not its tree path (#4455). `install_map()` maps each installed path to
its tree source, `stage()` copies the sources to that layout under a temporary root, and
`role_entries()` reads each command a role's tasks and templates run `--no-project` and
returns the staged copy it runs. `lib.stdlib_only` walks those entries from the stage.

Run: uv run pytest scripts/lib/tests/test_stdlib_only.py
"""

import ast
import atexit
import functools
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.repo_paths import ANSIBLE, REPO, ROLES

# A role command running a script `--no-project`: the first `.py` path after the flag, in one
# command. The scan stops at a blank line, a `-m` (a module run names no path) or the next
# `--no-project`, so it never takes another command's script. A path may hold `{{ var }}`, as
# in `{{ deploy_ui_dir }}/x.py`.
_NO_PROJECT = re.compile(r"--no-project\b")
_ROLE_RUN = re.compile(
    r"--no-project\b(?:(?!\n\s*\n|\s-m\s|--no-project\b)[\s\S]){0,400}?"
    r"((?:\{\{[^}]*\}\}|[^\s\"'{}])+\.py)\b"
)
_JINJA = re.compile(r"\{\{\s*([\w|. ]+?)\s*\}\}")


def _role_of(path: Path) -> Path:
    """The role directory `path` sits in: `ansible/roles/<plane>/<name>`."""
    plane, name = path.relative_to(ROLES).parts[:2]
    return ROLES / plane / name


@functools.cache
def _defaults(role: Path) -> dict[str, str]:
    """The role's `defaults/main.yml` scalars that hold no template of their own."""
    path = role / "defaults" / "main.yml"
    data = (
        yaml_fast.safe_load(path.read_text(encoding="utf-8"))
        if path.is_file()
        else None
    )
    return {
        k: v for k, v in (data or {}).items() if isinstance(v, str) and "{{" not in v
    }


def _render(text: str, role: Path, item: str | None = None) -> str:
    """`text` with the loop item, `role_path`, `playbook_dir` and the role's defaults filled in.

    Anything else stays as written, so a caller tells a resolved path by the absence of `{{`.
    """
    values = {
        **_defaults(role),
        "role_path": str(role),
        "playbook_dir": str(ANSIBLE),
    }
    if item is not None:
        values |= {"item": item, "item | basename": Path(item).name}

    def fill(match: re.Match) -> str:
        return values.get(match.group(1), match.group(0))

    return _JINJA.sub(fill, text)


def _yaml_dicts(node) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _yaml_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _yaml_dicts(value)


def _task_files() -> list[Path]:
    return sorted(ROLES.glob("*/*/tasks/**/*.yml"))


@functools.cache
def install_map() -> dict[str, tuple[Path, Path]]:
    """Each installed `.py` path -> (its source in the tree, the task file that installs it).

    Two declarations name an installed path: a `copy` task's `src`/`dest`, per loop item, and a
    `stamp_deployed_pairs` entry, which is how a role records the copies a shared include such
    as `install_host_lib.yml` makes for it. A `dest` the role's defaults cannot fill keeps its
    `{{ var }}`, so `role_entry` can still match it by name. Two sources for one path raise.
    """
    found: dict[str, tuple[Path, Path]] = {}

    def add(live: str, src: Path, task_file: Path) -> None:
        if not live.endswith(".py") or not src.is_file():
            return
        if live in found and found[live][0] != src.resolve():
            raise ValueError(
                f"{live} is installed from both {found[live][0]} and {src.resolve()}"
            )
        found.setdefault(live, (src.resolve(), task_file))

    for task_file in _task_files():
        role = _role_of(task_file)
        for node in _yaml_dicts(
            yaml_fast.safe_load(task_file.read_text(encoding="utf-8"))
        ):
            copy = node.get("ansible.builtin.copy") or node.get("copy")
            if isinstance(copy, dict) and "src" in copy and "dest" in copy:
                loop = node.get("loop")
                for item in loop if isinstance(loop, list) else [None]:
                    if item is not None and not isinstance(item, str):
                        continue
                    src = _render(str(copy["src"]), role, item)
                    if "{{" in src:
                        continue
                    path = (
                        Path(src) if Path(src).is_absolute() else role / "files" / src
                    )
                    add(_render(str(copy["dest"]), role, item), path, task_file)
            node_vars = node.get("vars")
            if isinstance(node_vars, dict):
                for pair in node_vars.get("stamp_deployed_pairs") or []:
                    if isinstance(pair, dict) and pair.get("live") and pair.get("src"):
                        add(
                            _render(str(pair["live"]), role),
                            REPO / pair["src"],
                            task_file,
                        )
    return found


def _under_stage(live: str) -> str:
    """Installed path `live` relative to the stage root; a `{{ var }}` segment becomes `var`."""
    return _JINJA.sub(r"\1", live).lstrip("/").replace(" ", "")


def staged_path(live: str) -> Path:
    """Where `stage()` puts installed path `live`."""
    return stage() / _under_stage(live)


@functools.cache
def stage() -> Path:
    """A temporary root holding a copy of every installed path at its host layout."""
    root = Path(tempfile.mkdtemp(prefix="stdlib-only-")).resolve()
    atexit.register(shutil.rmtree, root, True)
    for live, (src, _) in install_map().items():
        staged = root / _under_stage(live)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, staged)
    return root


def tree_source(path: Path) -> Path:
    """The tree file a staged copy was made from; any other path is its own source."""
    if not path.is_relative_to(stage()):
        return path
    sources = {staged_path(live): src for live, (src, _) in install_map().items()}
    return sources.get(path, path)


def role_entry(spelled: str, holder: Path) -> Path:
    """The file a role command runs: a tree path, or the staged copy of an installed one.

    A path the copy task does not spell resolves by name among the copies the same file
    makes: `release_bin.yml` runs its pruner through `current`, where it copied it to a
    per-SHA directory. Fails closed: a command that maps to no file raises.
    """
    role = _role_of(holder)
    path = _render(spelled, role)
    if "{{" not in path:
        if Path(path).is_relative_to(REPO) and Path(path).is_file():
            return Path(path).resolve()
        if path in install_map():
            return staged_path(path)
    same_file = [
        live
        for live, (_, source_file) in install_map().items()
        if source_file == holder and Path(live).name == Path(path).name
    ]
    if len(same_file) == 1:
        return staged_path(same_file[0])
    raise ValueError(
        f"{holder.relative_to(REPO)} runs {spelled} --no-project, and no copy task or "
        f"stamp_deployed_pairs entry installs it"
    )


def role_entries() -> list[tuple[Path, tuple[str, ...]]]:
    """Each script a role's task or template runs `--no-project`, run with no subcommand.

    No subcommand means every call-time import of the entry counts, which over-covers a
    script with two subcommands rather than parsing which one each cron passes.
    """
    found = []
    for path in sorted(ROLES.rglob("*")):
        if (
            path.suffix not in {".yml", ".yaml", ".j2"}
            or "tests" in path.relative_to(ROLES).parts
            or not path.is_file()
        ):
            continue
        found += [
            (role_entry(spelled, path), ())
            for spelled in _scripts_run(command_text(path), path)
        ]
    return found


def command_text(path: Path) -> str:
    """`path`'s text with its comment lines dropped.

    A comment line drops out rather than going blank: release_bin.yml comments inside its argv
    list, and a blank line ends a command.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("#", "{#"))
    )


def no_project_spans(text: str) -> list[tuple[int, int]]:
    """The span of each script path a `--no-project` command in `text` runs, as spelled."""
    found = []
    for flag in _NO_PROJECT.finditer(text):
        match = _ROLE_RUN.match(text, flag.start())
        if match is not None:
            found.append(match.span(1))
    return found


def _scripts_run(text: str, holder: Path) -> list[str]:
    """The script path each `--no-project` in `text` runs, as spelled.

    Raises:
        ValueError: a `--no-project` names no `.py` path the scan can read, such as a `-m`
            module run or a path further than the scan reaches. Discovery fails closed like
            the mapping does, so a command this cannot read never drops out of the set.
    """
    found = []
    for flag in _NO_PROJECT.finditer(text):
        match = _ROLE_RUN.match(text, flag.start())
        if match is None:
            line = text.count("\n", 0, flag.start()) + 1
            raise ValueError(
                f"{holder.relative_to(REPO)}: the --no-project command on non-comment "
                f"line {line} runs no .py path lib.role_installs can read"
            )
        found.append(match.group(1))
    return found


def installed(directory: Path) -> Path:
    """An out-of-tree directory's staged copy when `stage()` holds one, else `directory`.

    `render_records.py` inserts `/opt/gitops-deploy` to import the deployer's modules, which
    on the host are what that install directory holds, not the tree.
    """
    if directory.is_absolute() and not directory.is_relative_to(REPO):
        staged = stage() / directory.relative_to("/")
        if staged.is_dir():
            return staged
    return directory


@functools.cache
def staged_overrides() -> dict[str, str]:
    """Env names a staged script reads its install directory from, pointed at the stage.

    `live_drift_check.py` inserts `os.environ.get("HOST_LIB_DIR", "/opt/live-drift-check")`
    ahead of `PYTHONPATH`, so on a host that has the directory the import would load the
    installed copy instead of the staged one under test.
    """
    found = {}
    for live in install_map():
        for node in ast.walk(ast.parse(staged_path(live).read_text(encoding="utf-8"))):
            match node:
                case ast.Call(
                    func=ast.Attribute(value=ast.Attribute(attr="environ"), attr="get"),
                    args=[
                        ast.Constant(value=str() as name),
                        ast.Constant(value=str() as default),
                    ],
                ) if Path(default).is_absolute():
                    directory = installed(Path(default))
                    if directory.is_relative_to(stage()):
                        found[name] = str(directory)
    return found
