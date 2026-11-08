"""Whether a host already holds a changed setup file, read from the host itself.

The owed-host note derives reach from task gates, so it says which hosts a file lands on and
nothing about what they hold. A host another apply already reached still read as unapplied
(#7398). This answers that for the two kinds of file a host keeps a record of:

- `stamp_deployed.yml`'s `{live, src}` pairs name `copy:`-deployed files, which are
  byte-identical to their source, so the live bytes are compared with the merge commit's.
- `stamp_render.yml`'s fragments hold the source hash of each template the host last rendered,
  so the fragment's line is compared with the merge commit's hash. A fragment proves only the
  renders that run whenever it is written, so the line counts only when every task in the role
  that may render the template does (#7406). A stamp in a file a play in `ansible/*.yml` runs by
  `tasks_from:`, or in a file that one imports, proves no render, because that play's tags are
  not the walk's (#7411).

Both are read per host: a stamp or a render whose `when:` skips a host asks nothing of it. A
template every render of which is skipped on a host, with no stamp there, is held without a read,
which is how an import pinning `check_timer_state: absent` owes nothing. A gate reads the `vars:`
of the imports above it.

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
from typing import NamedTuple

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.narrow_lib.setup_playbook import play_role_includes
from deploy_tools.setup_lib.setup_gates import _eval_when
from diagnostics.probe_lib import core
from lib import yaml_fast
from lib.git import git_process
from lib.repo_paths import ALL_VARS, ANSIBLE, HOST_VARS

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
_NAME = re.compile(r"\b[A-Za-z_]\w*\b")
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


def _as_expression(value: object) -> str | None:
    """A task var as a Python expression a gate can hold, or None when it is not one."""
    if not isinstance(value, str):
        return None
    whole = _JINJA.fullmatch(value)
    if whole and "{{" not in whole[1]:
        return f"({whole[1]})"
    return None if "{{" in value else repr(value)


def _bound_by(task: dict, bound: dict[str, str]) -> dict[str, str]:
    """`bound` with `task`'s own `vars:` over it. A var with no expression stays unknown."""
    own = task.get("vars")
    if not isinstance(own, dict):
        return bound
    merged = {name: expr for name, expr in bound.items() if name not in own}
    for name, value in own.items():
        expr = _as_expression(value)
        if expr is not None:
            merged[name] = expr
    return merged


def _bound_gate(gate: object, bound: dict[str, str]) -> object:
    """`gate` with each name `bound` holds replaced by its expression."""
    if isinstance(gate, list):
        return [_bound_gate(item, bound) for item in gate]
    if not isinstance(gate, str):
        return gate
    return _NAME.sub(lambda name: bound.get(name[0], name[0]), gate)


class _Walked(NamedTuple):
    """One task as the walk reaches it: its effective tags and each `when:` above and on it.

    `source` is the task file that holds it.
    """

    task: dict
    tags: frozenset[str]
    gates: tuple
    source: Path


class _Stamp(NamedTuple):
    """One stamp task's record. `tags` is None when the walk cannot know the full tags."""

    record: str
    tags: frozenset[str] | None
    gates: tuple


class _Render(NamedTuple):
    tags: frozenset[str]
    gates: tuple


class _Evidence(NamedTuple):
    """What a role's tasks say about one changed file."""

    stamps: list[_Stamp]
    renders: list[_Render]


def _flatten(
    tasks: object,
    inherited: frozenset[str],
    role_dir: Path,
    on_walk: tuple[Path, ...],
    seen: set[Path],
    gates: tuple = (),
    bound: dict[str, str] | None = None,
) -> Iterable[_Walked]:
    """Each task with its effective tags and gates, through blocks and static imports.

    An import's tags, gates and `vars:` reach the imported tasks, so one task file imported twice
    yields its tasks twice, once per import site.
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
        scope = _bound_by(task, bound or {})
        chain = (*gates, _bound_gate(task["when"], scope)) if "when" in task else gates
        yield _Walked(task, tags, chain, on_walk[-1])
        for section in ("block", "rescue", "always"):
            yield from _flatten(
                task.get(section), tags, role_dir, on_walk, seen, chain, scope
            )
        target = _imported_file(task, role_dir)
        if target and target not in on_walk:
            seen.add(target)
            yield from _flatten(
                _parsed_tasks(target),
                tags,
                role_dir,
                (*on_walk, target),
                seen,
                chain,
                scope,
            )


def _role_tasks(role_dir: Path) -> tuple[list[_Walked], list[_Walked]]:
    """A role's tasks: those `tasks/main.yml` reaches, then those in any other file.

    The second list's tags are a lower bound, because a file `main.yml` does not reach runs
    under tags this walk cannot read, such as a `tasks_from` play's. `_tasks_from_files` names
    the files in the first list that carry the same doubt.
    """
    role_dir = role_dir.resolve()
    main = role_dir / "tasks" / "main.yml"
    seen = {main}
    from_main = (
        list(_flatten(_parsed_tasks(main), frozenset(), role_dir, (main,), seen))
        if main.is_file()
        else []
    )
    others: list[_Walked] = []
    for path in sorted(role_dir.glob("tasks/**/*.yml")):
        path = path.resolve()
        if path not in seen:
            seen.add(path)
            others += _flatten(
                _parsed_tasks(path), frozenset(), role_dir, (path,), seen
            )
    return from_main, others


def _tasks_from_files(role_dir: Path, playbooks_dir: Path) -> frozenset[Path]:
    """The task files a play in `playbooks_dir` runs by `tasks_from:`, with each file they import.

    Such a file also runs under the including play task's tags, which `main.yml`'s walk never
    reads. A templated `tasks_from:` is not followed, as `play_role_includes` leaves it out.
    """
    role_dir = role_dir.resolve()
    found: set[Path] = set()
    for play in sorted(playbooks_dir.glob("*.yml")):
        for name, _ in play_role_includes(play.read_text(), role_dir.name):
            entry = (role_dir / "tasks" / name).with_suffix(Path(name).suffix or ".yml")
            if not entry.is_file() or entry.resolve() in found:
                continue
            entry = entry.resolve()
            seen = {entry}
            for _ in _flatten(
                _parsed_tasks(entry), frozenset(), role_dir, (entry,), seen
            ):
                pass
            found |= seen
    return frozenset(found)


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


def _every_render_stamped(renders: list[_Render], fragments: list[_Stamp]) -> bool:
    """Does writing some fragment on this host run each task that may render there?

    A stamp task whose tags are a subset of a render task's cannot run under `--tags` without
    that render. Any other pair can, which leaves the fragment current and the render stale.
    """
    groups = [stamp.tags for stamp in fragments if stamp.tags is not None]
    return bool(renders) and all(
        any(group <= render.tags for group in groups) for render in renders
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
      all_vars: the `group_vars/all.yml` a stamp's or a render's `when:` reads.
      host_vars_dir: the `host_vars/` directory that overrides it.
      playbooks_dir: the directory whose `*.yml` plays may run a role's file by `tasks_from:`.
    """

    def __init__(
        self,
        files: Iterable[str],
        roles_dir: Path,
        ref: str,
        repo: Path,
        read_host: Callable[[dict, str], dict],
        all_vars: Path = ALL_VARS,
        host_vars_dir: Path = HOST_VARS,
        playbooks_dir: Path = ANSIBLE,
    ) -> None:
        self._files = frozenset(files)
        self._roles_dir = roles_dir
        self._ref = ref
        self._repo = repo
        self._read_host = read_host
        self._all_vars = all_vars
        self._host_vars_dir = host_vars_dir
        self._playbooks_dir = playbooks_dir
        self._docs: dict[str, dict] = {}

    @cached_property
    def _evidence(self) -> dict[tuple[str, str], _Evidence]:
        """`(role, src)` -> how the role stamps and renders a changed file it touches."""
        found: dict[tuple[str, str], _Evidence] = {}
        for role_dir in self._roles_dir.iterdir():
            try:
                from_main, others = _role_tasks(role_dir)
                entries = _tasks_from_files(role_dir, self._playbooks_dir)
            except _CannotWalk:
                continue
            walked = from_main + others
            for src in self._files:
                renders = [
                    _Render(w.tags, w.gates) for w in walked if _may_render(w.task, src)
                ]
                if renders:
                    found[(role_dir.name, src)] = _Evidence([], renders)
            for tasks, from_main_only in ((from_main, True), (others, False)):
                for task, tags, gates, source in tasks:
                    tags_known = from_main_only and source not in entries
                    for src, record in _stamps_of(task):
                        if src not in self._files:
                            continue
                        evidence = found.setdefault(
                            (role_dir.name, src), _Evidence([], [])
                        )
                        # A stamp whose full tags are unknown proves no render.
                        evidence.stamps.append(
                            _Stamp(record, tags if tags_known else None, gates)
                        )
        return found

    def _runs_on(self, role: str, gates: tuple, host: str) -> bool:
        """Do these gates pass on `host`? A gate this read cannot evaluate passes."""
        defaults = self._roles_dir / role / "defaults" / "main.yml"
        return all(
            _eval_when(gate, host, self._all_vars, self._host_vars_dir, defaults)
            for gate in gates
        )

    @cached_property
    def _shas(self) -> dict[str, str]:
        """Each stamped changed file's SHA-256 at `ref`; a file `ref` lacks has no entry."""
        shas = {}
        for src in {src for _, src in self._evidence}:
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
                stamp.record: self._shas[src]
                for (_, src), evidence in self._evidence.items()
                for stamp in evidence.stamps
                if src in self._shas and "{{" not in stamp.record
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

        A stamp or a render whose `when:` skips `host` writes nothing there, so it asks nothing
        of the host, and a template with neither on `host` is held. False for any other file with
        no stamp on `host`, for one whose record is missing or unread, and for a render on `host`
        that no fragment written there proves.
        """
        evidence = self._evidence.get((role, path))
        if not evidence:
            return False
        stamps = [s for s in evidence.stamps if self._runs_on(role, s.gates, host)]
        renders = [r for r in evidence.renders if self._runs_on(role, r.gates, host)]
        # `.j2` only: a templated `src` may match any path, and a copied file is no render.
        if path.endswith(".j2") and evidence.renders and not renders and not stamps:
            return True
        sha = self._shas.get(path)
        if not stamps or not sha:
            return False
        fragments = [s for s in stamps if s.record.startswith(RENDER_MANIFEST_DIR)]
        if fragments and not _every_render_stamped(renders, fragments):
            return False
        doc = self._doc_on(host)
        return all(_record_shows(doc, stamp.record, path, sha) for stamp in stamps)
