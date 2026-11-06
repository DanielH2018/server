"""Whether a host already holds a changed setup file, read from the host itself.

The owed-host note derives reach from task gates, so it says which hosts a file lands on and
nothing about what they hold. A host another apply already reached still read as unapplied
(#7398). This answers that for the two kinds of file a host keeps a record of:

- `stamp_deployed.yml`'s `{live, src}` pairs name `copy:`-deployed files, which are
  byte-identical to their source, so the live bytes are compared with the merge commit's.
- `stamp_render.yml`'s fragments hold the source hash of each template the host last rendered,
  so the fragment's line is compared with the merge commit's hash. A fragment proves only the
  renders that run whenever it is written, so the line counts only when every task in the role
  that may render the template does (#7406).

Everything else stays owed: an unstamped file, a task file, a key, a pair whose `live` is not a
literal path, a template with a render no fragment proves, a role whose imports this walk cannot
follow, a host that cannot be read. The note is then what it was without this read, which costs
one hand check; the other direction would hide an apply a host still needs.
"""

import hashlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from functools import cached_property
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from diagnostics.probe_lib import core
from lib import yaml_fast
from lib.git import git_process

GIT_TIMEOUT_S = 30
# `stamp_render.yml`'s directory on a host.
RENDER_MANIFEST_DIR = "/var/lib/homelab/setup-render-manifest.d/"

_IMPORTS = (
    "import_tasks",
    "ansible.builtin.import_tasks",
    "ansible.legacy.import_tasks",
)
_UNFOLLOWED = tuple(
    f"{prefix}{module}"
    for prefix in ("", "ansible.builtin.", "ansible.legacy.")
    for module in ("include_tasks", "include_role", "import_role")
)
_TEMPLATE_MODULES = ("template", "ansible.builtin.template", "ansible.legacy.template")
_JINJA = re.compile(r"{{\s*(.*?)\s*}}")
_UNKNOWN = "\0"


class _CannotWalk(Exception):
    """A role pulls in a task file this walk cannot follow, so its renders are unknown."""


def _module_args(task: dict, spellings: tuple[str, ...]) -> object:
    return next((task[key] for key in spellings if key in task), None)


def _imported_file(task: dict, role_dir: Path) -> Path | None:
    """The file a task imports, or None for a task that imports nothing.

    Raises:
      _CannotWalk: the task pulls in a file by a form or a name this walk cannot resolve.
    """
    if any(key in task for key in _UNFOLLOWED):
        raise _CannotWalk(f"{task.get('name')} pulls in tasks dynamically")
    args = _module_args(task, _IMPORTS)
    if args is None:
        return None
    name = args.get("file") if isinstance(args, dict) else args
    resolved = str(name).replace("{{ role_path }}", str(role_dir))
    target = (role_dir / "tasks" / resolved).resolve()
    if "{{" in resolved or not target.is_file():
        raise _CannotWalk(f"cannot resolve the import of {name}")
    return target


def _parsed_tasks(path: Path) -> list:
    try:
        tasks = yaml_fast.parse_yaml(path.read_text())
    except yaml_fast.UnparsableYaml, OSError:
        raise _CannotWalk(f"cannot read {path}") from None
    return tasks if isinstance(tasks, list) else []


def _flatten(
    tasks: object,
    inherited: frozenset[str],
    role_dir: Path,
    on_walk: tuple[Path, ...],
    seen: set[Path],
) -> Iterable[tuple[dict, frozenset[str]]]:
    """Each task with its effective tags, through blocks and static imports.

    An import's tags reach the imported tasks, so one task file imported twice yields its tasks
    twice, once per import site.
    """
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        own = task.get("tags") or []
        tags = inherited | frozenset(
            str(t) for t in ([own] if isinstance(own, str) else own)
        )
        yield task, tags
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section), tags, role_dir, on_walk, seen)
        target = _imported_file(task, role_dir)
        if target and target not in on_walk:
            seen.add(target)
            yield from _flatten(
                _parsed_tasks(target), tags, role_dir, (*on_walk, target), seen
            )


def _role_tasks(
    role_dir: Path,
) -> tuple[list[tuple[dict, frozenset[str]]], list[tuple[dict, frozenset[str]]]]:
    """A role's tasks: those `tasks/main.yml` reaches, then those in any other file.

    The second list's tags are a lower bound, because a file `main.yml` does not reach runs
    under tags this walk cannot read, such as a `tasks_from` play's.
    """
    role_dir = role_dir.resolve()
    main = role_dir / "tasks" / "main.yml"
    seen = {main}
    from_main = (
        list(_flatten(_parsed_tasks(main), frozenset(), role_dir, (main,), seen))
        if main.is_file()
        else []
    )
    others: list[tuple[dict, frozenset[str]]] = []
    for path in sorted(role_dir.glob("tasks/**/*.yml")):
        path = path.resolve()
        if path not in seen:
            seen.add(path)
            others += _flatten(
                _parsed_tasks(path), frozenset(), role_dir, (path,), seen
            )
    return from_main, others


def _stamps_of(task: dict) -> Iterable[tuple[str, str]]:
    """Each `(src, record)` one task stamps.

    `record` is the path of the live copy, or of the render fragment under
    `RENDER_MANIFEST_DIR` that lists the template.
    """
    task_vars = task.get("vars")
    if not isinstance(task_vars, dict):
        return
    for pair in task_vars.get("stamp_deployed_pairs") or ():
        if isinstance(pair, dict):
            yield str(pair.get("src")), str(pair.get("live"))
    templates = task_vars.get("stamp_render_templates")
    for template in templates if isinstance(templates, list) else ():
        yield (
            str(template),
            f"{RENDER_MANIFEST_DIR}{task_vars.get('stamp_render_name')}",
        )


def _may_render(task: dict, template: str) -> bool:
    """Could this task render `template`? True when a templated `src` could name it.

    A `{{ item }}` over a literal `loop:` expands. Any other expression matches anything.
    """
    args = _module_args(task, _TEMPLATE_MODULES)
    if args is None:
        return False
    src = args.get("src") if isinstance(args, dict) else None
    if not isinstance(src, str):
        return True
    loop = task.get("loop")
    items = loop if isinstance(loop, list) else (None,)
    return any(_src_names(src, item, template) for item in items)


def _src_names(src: str, item: object, template: str) -> bool:
    """Could `src`, with `item` as the loop value or None, name `template`? Basenames decide."""

    def value(match: re.Match) -> str:
        return str(item) if item is not None and match[1] == "item" else _UNKNOWN

    literals = _JINJA.sub(value, src).rsplit("/", 1)[-1].split(_UNKNOWN)
    pattern = ".*".join(map(re.escape, literals))
    return re.fullmatch(pattern, template.rsplit("/", 1)[-1]) is not None


def _every_render_stamped(
    template: str,
    stamp_tags: Iterable[frozenset[str]],
    tasks: list[tuple[dict, frozenset[str]]],
) -> bool:
    """Does writing some fragment listing `template` run each task that may render it?

    A stamp task whose tags are a subset of a render task's cannot run under `--tags` without
    that render. Any other pair can, which leaves the fragment current and the render stale.
    """
    renders = [tags for task, tags in tasks if _may_render(task, template)]
    groups = list(stamp_tags)
    return bool(renders) and all(
        any(group <= tags for group in groups) for tags in renders
    )


def _record_shows(doc: dict, record: str, src: str, sha: str) -> bool:
    """Does `record`, as `host_reader` read it into `doc`, show `src` at hash `sha`?"""
    if record.startswith(RENDER_MANIFEST_DIR):
        row = (doc.get("files") or {}).get(record) or {}
        return f"{src} {sha}" in row.get("text", "").splitlines()
    row = (doc.get("stat") or {}).get(record) or {}
    if row.get("status") != "ok":
        return False
    return row.get("sha256") == sha or row.get("match") is True


class DeployedCopies:
    """What each host records of one landing's changed files, read once per host.

    `ufw limit ssh` refuses a sixth connection in 30 seconds, so the first question about a
    host reads every record this landing could ask about in one `read_host` call.

    Args:
      files: the changed paths.
      roles_dir: the setup roles at `ref`, which say where each file is deployed.
      ref: the merge commit, whose bytes a live copy has to match.
      repo: a checkout whose git store holds `ref`.
      read_host: `host_reader.read_host`.
    """

    def __init__(
        self,
        files: Iterable[str],
        roles_dir: Path,
        ref: str,
        repo: Path,
        read_host: Callable[[dict, str], dict],
    ) -> None:
        self._files = frozenset(files)
        self._roles_dir = roles_dir
        self._ref = ref
        self._repo = repo
        self._read_host = read_host
        self._docs: dict[str, dict] = {}

    @cached_property
    def _records(self) -> dict[tuple[str, str], list[str]]:
        """`(role, src)` -> each record of that changed file the role stamps on a host."""
        found: dict[tuple[str, str], list[str]] = {}
        for role_dir in self._roles_dir.iterdir():
            try:
                from_main, others = _role_tasks(role_dir)
            except _CannotWalk:
                continue
            fragment_tags: dict[str, list[frozenset[str]]] = {}
            for tasks, tags_known in ((from_main, True), (others, False)):
                for task, tags in tasks:
                    for src, record in _stamps_of(task):
                        if src not in self._files:
                            continue
                        found.setdefault((role_dir.name, src), []).append(record)
                        if record.startswith(RENDER_MANIFEST_DIR):
                            # A stamp whose full tags are unknown proves no render.
                            proving = [tags] if tags_known else []
                            fragment_tags.setdefault(src, []).extend(proving)
            for src, stamps in fragment_tags.items():
                if not _every_render_stamped(src, stamps, from_main + others):
                    del found[role_dir.name, src]
        return found

    @cached_property
    def _shas(self) -> dict[str, str]:
        """Each stamped changed file's SHA-256 at `ref`; a file `ref` lacks has no entry."""
        shas = {}
        for src in {src for _, src in self._records}:
            # `cat-file`, because a textconv or a newline translation would change the hash.
            blob = git_process("cat-file", "blob", f"{self._ref}:{src}", cwd=self._repo)
            try:
                content, _ = blob.communicate(timeout=GIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                blob.kill()
                blob.wait()
                continue
            if blob.returncode == 0:
                shas[src] = hashlib.sha256(content).hexdigest()
        return shas

    def _doc_on(self, host: str) -> dict:
        if host not in self._docs:
            shas_by_record = {
                record: self._shas[src]
                for (_, src), records in self._records.items()
                for record in records
                if src in self._shas and "{{" not in record
            }
            fragments = {r for r in shas_by_record if r.startswith(RENDER_MANIFEST_DIR)}
            asks = {r: h for r, h in shas_by_record.items() if r not in fragments}
            plan = {"stat": sorted(asks), "rootq": asks, "files": sorted(fragments)}
            try:
                self._docs[host] = self._read_host(plan, host)
            except core.Unreadable as exc:
                print(
                    f"could not read what {host} holds ({exc.message}), so its copies count "
                    "as unapplied",
                    file=sys.stderr,
                )
                self._docs[host] = {}
        return self._docs[host]

    def holds(self, role: str, path: str, host: str) -> bool:
        """Does every record `role` stamps of `path` on `host` show the merge commit's bytes?

        False for a file with no stamp, and for one whose record is missing or unread.
        """
        records = self._records.get((role, path))
        sha = self._shas.get(path)
        if not records or not sha:
            return False
        doc = self._doc_on(host)
        return all(_record_shows(doc, record, path, sha) for record in records)
