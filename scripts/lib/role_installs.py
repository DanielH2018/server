"""Where a host installs each role script, and which of them a host runs `--no-project`.

A role script runs from the path a host component's `files` entry installs it to, so its
`sys.path` is the install directory, not its tree path (#4455). `install_map()` maps each
installed path to its tree source, `stage()` copies the sources to that layout under a temporary root, and
`role_entries()` reads each command a role's or component's files run `--no-project` and
returns the staged copy it runs. `lib.stdlib_only` walks those entries from the stage.

Run: uv run pytest scripts/lib/tests/test_stdlib_only.py
"""

import atexit
import functools
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.repo_paths import ANSIBLE, REPO, ROLES
from renderer.renderer_lib.host_sources import SHARED_FILES

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
COMPONENTS = REPO / "hosts" / "components"


def _role_of(path: Path) -> Path:
    """The role or component directory `path` sits in."""
    if path.is_relative_to(COMPONENTS):
        return COMPONENTS / path.relative_to(COMPONENTS).parts[0]
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


def _render(text: str, role: Path) -> str:
    """`text` with `role_path`, `playbook_dir` and the role's defaults filled in.

    Anything else stays as written, so a caller tells a resolved path by the absence of `{{`.
    """
    values = {
        **_defaults(role),
        "role_path": str(role),
        "playbook_dir": str(ANSIBLE),
    }

    def fill(match: re.Match) -> str:
        return values.get(match.group(1), match.group(0))

    return _JINJA.sub(fill, text)


@functools.cache
def install_map() -> dict[str, tuple[Path, Path]]:
    """Each installed `.py` path -> (its source in the tree, the `component.yml` that installs it).

    Two sources for one path raise.
    """
    found: dict[str, tuple[Path, Path]] = {}

    def add(live: str, src: Path, declared_in: Path) -> None:
        if not live.endswith(".py") or not src.is_file():
            return
        if live in found and found[live][0] != src.resolve():
            raise ValueError(
                f"{live} is installed from both {found[live][0]} and {src.resolve()}"
            )
        found.setdefault(live, (src.resolve(), declared_in))

    # A host component installs a script by a `src:` or `role-file:` entry, and a shared
    # module beside it by a `lib:` or `shared:` entry.
    for component in sorted(COMPONENTS.glob("*/component.yml")):
        data = yaml_fast.safe_load(component.read_text(encoding="utf-8")) or {}
        role_dirs = sorted(ROLES.glob(f"*/{data.get('role')}"))
        for entry in data.get("files") or []:
            src = str(entry.get("src", ""))
            if src.endswith(".py") and ":" not in src:
                add(entry["dst"], component.parent / src, component)
            name = src.removeprefix("role-file:")
            if name != src and role_dirs:
                add(entry["dst"], role_dirs[0] / "files" / name, component)
            if entry.get("lib"):
                add(entry["dst"], REPO / "hosts" / "lib" / entry["lib"], component)
            if entry.get("shared") in SHARED_FILES:
                add(entry["dst"], REPO / SHARED_FILES[entry["shared"]], component)
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

    A path no `files` entry spells resolves by name among the files the same `component.yml`
    installs. Fails closed: a command that maps to no file raises.
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
        f"{holder.relative_to(REPO)} runs {spelled} --no-project, and no component "
        f"`files` entry installs it"
    )


def role_entries() -> list[tuple[Path, tuple[str, ...]]]:
    """Each script a role or a component runs `--no-project`, run with no subcommand.

    No subcommand means every call-time import of the entry counts, which over-covers a
    script with two subcommands rather than parsing which one each cron passes.
    """
    found = []
    for path in sorted([*ROLES.rglob("*"), *COMPONENTS.rglob("*")]):
        if (
            path.suffix not in {".yml", ".yaml", ".j2"}
            or "tests" in path.parts
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

    A comment line drops out rather than going blank: a task may comment inside its argv list,
    and a blank line ends a command.
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

    A script that inserts an absolute install directory imports what that directory holds on
    the host, not the tree.
    """
    if directory.is_absolute() and not directory.is_relative_to(REPO):
        staged = stage() / directory.relative_to("/")
        if staged.is_dir():
            return staged
    return directory
