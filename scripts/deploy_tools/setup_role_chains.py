#!/usr/bin/env python3
"""Reading a setup role's `tasks/` tree for the `when:` chains a changed file sits behind.

`land_reach` asks two questions of a role: which hosts run any of it (`setup_role_hosts`),
and which hosts a single changed FILE lands on (`setup_file_hosts`). Both answers come from
the same traversal -- walk `tasks/main.yml` through its static imports and `block:` bodies,
and collect the `when:` chain on every leaf task that counts as evidence for the file at
hand. This module is that traversal; `land_reach` evaluates the chains it returns against
each host's vars and owns the operator-facing note.

A changed file's chains narrow further to the tasks the PR range changed (#3976): a box-only
cron edit in `initial_setup/tasks/crons.yml` read as reaching every host the file's ungated
crons run on. `task_file_chains` reads the range from git at both ends, and every answer it
gives falls back to the whole file on doubt, the same asymmetry as the traversal's.

Every entry point here returns None when it finds no evidence, so `land_reach` keeps the
role-level answer rather than narrowing. Narrower than the truth hides an unconverged host,
where wider costs an operator one no-op command.

Split out of `land_reach.py` at the module-length cap.
"""

import contextlib
import functools
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Generator
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.git import git

# include_tasks and import_tasks read alike here: the only place they diverge is a
# runtime-templated target (`include_tasks: "{{ var }}.yml"`), and _gates_in already skips
# that case (`"{{" in target`) rather than following it. A literal include_tasks --
# `include_tasks: install.yml` under a `when:` -- carries that when: to the file it pulls in
# the same way a static import_tasks does.
_IMPORT_KEYS = (
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
)
SHIPPED_DIRS = ("templates", "files")


def task_chains(
    role_dir: Path, keep, task_file: str = "main.yml", inherited: tuple = ()
) -> list[tuple] | None:
    """Every `when:` chain (import and block gates, then its own) on each leaf `keep` accepts.

    Reads the role's `tasks/` tree from `task_file` through its static imports and `block:`
    bodies. A leaf is a task that neither imports another file nor opens a block;
    `keep(task, task_file)` is called with the leaf and the basename of the task file it
    sits in, and every accepted leaf contributes one chain. Returns None when `task_file`
    cannot be read, so the caller falls back rather than narrows; an unreadable file
    further down the import tree contributes nothing, as before.
    """
    path = role_dir / "tasks" / task_file
    try:
        tasks = _parse_tasks(path, path.stat().st_mtime_ns)
    except OSError, yaml.YAMLError:
        return None
    if tasks is None:
        return None
    return _gates_in(tasks, role_dir, keep, task_file, inherited)


@functools.lru_cache(maxsize=256)
def _parse_tasks(path: Path, _mtime_ns: int) -> list | None:
    """`path` parsed once per content, mirroring `setup_gates._parse_vars_file`'s cache key.

    `setup_repo_file_hosts` asks every setup role whether it ships one changed repo file, so
    a PR with several of them walks the same 18 task trees once per file. The cache makes
    that one parse per file on disk rather than one per question; a rewrite changes the
    mtime and misses. Returns None for a tasks file that parses to something other than a
    list, which `task_chains` reads the same way as an unreadable one.
    """
    tasks = yaml_fast.safe_load(path.read_text()) or []
    return tasks if isinstance(tasks, list) else None


def task_gates_naming(role_dir: Path, basename: str) -> list[tuple] | None:
    """Every `when:` chain on a task naming `basename`.

    A task names the file when the string appears anywhere in its body -- `src:`, a
    `loop:` item, a `lookup('file', ...)` -- matched by basename, which is how every
    `template`/`copy` task in this tree refers to what it ships. `_ships_via_loop` covers
    the one shape that literal match cannot: a loop of bare names templated into `src:
    "{{ item }}.j2"`, where the shipped file's basename carries the suffix the loop items
    do not.
    """
    return task_chains(
        role_dir,
        lambda task, _: basename in json.dumps(task) or _ships_via_loop(task, basename),
    )


def task_gates_shipping_repo_path(role_dir: Path, repo_path: str) -> list[tuple]:
    """Every `when:` chain on a task that ships `repo_path`, a file OUTSIDE this role.

    Two setup-role tasks copy a file from the repo checkout rather than from the role's own
    `templates/`/`files/`, with `src: "{{ playbook_dir }}/../<repo-relative path>"`:
    hypervisor's etcd drill script and `common`'s release pruner. `setup_file_hosts` cannot see
    either of them, because it keys on the path being under `ansible/roles/setup/<role>/`.

    The match is the repo-relative PATH, not the basename `task_gates_naming` uses. A
    basename is the right key for a file under the role's own directory, where the role
    already owns it; for a repo file, any of 18 roles could mention `deploy.sh` in a comment
    and claim to ship it. The path literal appears whole inside the `src:` above, so it is
    both sufficient and far narrower.

    Returns an EMPTY list, not None, when no task ships the path -- the opposite asymmetry
    to the rest of this module, and deliberately. Here absence of evidence means the role
    does not ship the file, so the file owes it nothing; falling back to the role-level
    reach would make every unrelated repo path in a PR (`scripts/dev/pytest_shard_weights.json`,
    a test file) widen every changed setup role to every host it reaches.
    """
    return task_chains(role_dir, lambda task, _: repo_path in json.dumps(task)) or []


def _ships_via_loop(task: dict, basename: str) -> bool:
    """Whether `task` ships `basename` through a `loop:` of bare names and `{{ item }}`.

    `basename in json.dumps(task)` misses gitops-deploy's systemd-unit shape: `src: "{{
    item }}.j2"` over `loop: [gitops-deploy.service, ...]` never puts the literal string
    `gitops-deploy.timer.j2` (the shipped file's basename, under `templates/`) anywhere in
    the task's own text -- the loop items are the bare unit names, without the `.j2` `src`
    appends.

    An exact loop-item match (no suffix involved) only needs `{{ item }}` present, same as
    the existing literal-substring check one level up. The `.j2`-stripped match needs more:
    `{{ item }}.j2` itself, not just `{{ item }}` anywhere, or this also matches a task that
    loops over the SAME bare names for an unrelated reason -- this role's own teardown
    removes `gitops-deploy.timer` by `path: "/etc/systemd/system/{{ item }}"`, no `.j2`
    anywhere, and a looser check misread that removal as also shipping the template.
    """
    loop = task.get("loop")
    if not isinstance(loop, list) or not all(isinstance(item, str) for item in loop):
        return False
    text = json.dumps(task)
    if basename in loop and "{{ item }}" in text:
        return True
    stem = basename.removesuffix(".j2")
    return stem != basename and stem in loop and "{{ item }}.j2" in text


def _gates_in(
    tasks, role_dir: Path, keep, task_file: str, inherited: tuple
) -> list[tuple]:
    found: list[tuple] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        chain = inherited + ((task["when"],) if "when" in task else ())
        target = next((task[k] for k in _IMPORT_KEYS if k in task), None)
        if isinstance(target, str):
            if "{{" in target:
                # A cross-role import (`{{ role_path }}/../common/tasks/...`): nothing the
                # imported file ships is this role's, so it is not followed. The importing
                # task is still offered as a leaf, because its own `vars:` block is where
                # this role feeds the shared file -- gitops_deploy's install.yml passes
                # `gitops_deploy_ruleset_drift_cron_hour` to `kuma_check_timer.yml` that
                # way, and `var_consumer_chains` finds no consumer without it.
                if keep(task, task_file):
                    found.append(chain)
                continue
            found.extend(task_chains(role_dir, keep, Path(target).name, chain) or [])
        elif "block" in task:
            found.extend(_gates_in(task["block"], role_dir, keep, task_file, chain))
        elif keep(task, task_file):
            found.append(chain)
    return found


VARS_DIRS = ("defaults", "vars")


def _leaf_chains_with_text(role_dir: Path) -> list[tuple[str, tuple]] | None:
    """Each leaf task's JSON text paired with its `when:` chain, or None if unreadable.

    `task_chains` appends one chain per leaf `keep` accepts, in traversal order, so a
    `keep` that records what it is handed and accepts everything pairs positionally with
    the chains it gets back.
    """
    texts: list[str] = []

    def keep(task, _task_file) -> bool:
        texts.append(json.dumps(task))
        return True

    chains = task_chains(role_dir, keep)
    return None if chains is None else list(zip(texts, chains, strict=True))


def _shipped_texts(role_dir: Path) -> dict[str, str]:
    """{basename: text} for each readable file under the role's `templates/` and `files/`."""
    texts: dict[str, str] = {}
    for sub in SHIPPED_DIRS:
        for path in sorted((role_dir / sub).glob("*")):
            if not path.is_file():
                continue
            try:
                texts[path.name] = path.read_text()
            except OSError, UnicodeDecodeError:
                continue
    return texts


def var_consumer_chains(role_dir: Path, vars_file: Path) -> list[tuple] | None:
    """Every `when:` chain on a task that consumes a var `vars_file` defines.

    A `defaults/main.yml` is not itself shipped, so no task names it and `setup_file_hosts`
    fell back to the role-level reach -- which for a `has_gitops` dispatcher is all three
    hosts. For example `gitops_deploy/defaults/main.yml` must not prescribe
    `initial_setup.yml --tags gitops_deploy` on daniel-server and daniel-pi, where the role
    runs `teardown.yml` alone and every one of those vars is read by `install.yml`. A var
    reaches the hosts that run a task consuming it, so this collects the chain on each
    consumer: a leaf task naming the var, or a `templates/`/`files/` file naming it, under
    the chain of the task that ships that file.

    Returns None -- caller keeps the role-level answer -- unless EVERY var defined here has
    at least one consumer. One var resolved by textual search says nothing about the next,
    and a var consumed only through a shape this cannot read (a `hostvars` lookup, a cron
    the deployer's Python reads at runtime) would otherwise narrow the whole file on absence
    of evidence. That is the failure `_eval_when`'s docstring names: narrower than the truth
    hides an unconverged host, where wider costs an operator one no-op command.
    """
    try:
        defined = yaml_fast.safe_load(vars_file.read_text())
    except OSError, yaml.YAMLError:
        return None
    leaves = _leaf_chains_with_text(role_dir)
    if not isinstance(defined, dict) or not defined or leaves is None:
        return None
    shipped = _shipped_texts(role_dir)
    shipping_chains: dict[str, list[tuple]] = {}
    chains: list[tuple] = []
    for name in defined:
        # Whole word, so `gitops_deploy_staging_gate` is not counted as consumed by a task
        # that only names `gitops_deploy_staging_gate_blocking` -- coverage passing on a
        # longer sibling's chains is the one way this gate narrows on the wrong evidence.
        named = re.compile(rf"\b{re.escape(name)}\b").search
        consumers = [chain for text, chain in leaves if named(text)]
        for basename, text in shipped.items():
            if not named(text):
                continue
            if basename not in shipping_chains:
                shipping_chains[basename] = task_gates_naming(role_dir, basename) or []
            consumers.extend(shipping_chains[basename])
        if not consumers:
            return None
        chains.extend(consumers)
    return chains


def _notified_names(task: dict) -> set[str]:
    """Every handler name `task` notifies, by its own `notify:` or through a `vars:` value.

    `notify:` is a string or a list of strings. The `vars:` arm covers the one indirect
    shape in this tree: `gitops_deploy/tasks/install.yml` imports the shared
    `common/tasks/kuma_check_timer.yml` with `host_lib_notify: [Run gitops-deploy once]`,
    so the importing task -- which `_gates_in` offers as a leaf, because a cross-role
    import is not followed -- is where that notify's gate lives. Counting it is the wider
    answer, and wider is the safe direction here.

    A `vars:` value counts only when it is a LIST, the shape every such handoff in this
    tree uses. A bare string var that happened to hold a handler's name would otherwise
    supply a false notifier, and a false notifier NARROWS -- the one direction this module
    must not get wrong.
    """
    names: set[str] = set()
    notify = task.get("notify")
    if isinstance(notify, str):
        names.add(notify)
    elif isinstance(notify, list):
        names.update(item for item in notify if isinstance(item, str))
    holder = task.get("vars")
    for value in holder.values() if isinstance(holder, dict) else ():
        if isinstance(value, list):
            names.update(item for item in value if isinstance(item, str))
    return names


def handler_notifier_chains(role_dir: Path, handlers_file: Path) -> list[tuple] | None:
    """Every `when:` chain on a task notifying a handler `handlers_file` defines.

    A handler is shipped nowhere and no task names its file, so a `handlers/` path fell
    through to the role-level reach -- which for a `has_gitops` dispatcher like
    `gitops_deploy` is the union of both halves, all three hosts. A handler runs
    where the tasks that `notify:` it run, so this collects the chain on each notifying
    leaf task.

    Matches a handler name exactly, never as a substring: `docker_install` defines `Reload
    systemd after docker teardown`, and a substring test would let that satisfy another
    role's `Reload systemd`.

    Keeps `var_consumer_chains`'s asymmetry. Returns None -- caller keeps the role-level
    answer -- unless EVERY handler defined here has at least one notifying task, so a
    handler notified through a shape this cannot read stays wide rather than narrowing the
    file on absence of evidence. `listen:` topics are not read: no setup role uses one,
    and a handler that only listens has no `name:` consumer to find, which returns None on
    its own.
    """
    try:
        defined = yaml_fast.safe_load(handlers_file.read_text())
    except OSError, yaml.YAMLError:
        return None
    if not isinstance(defined, list) or not defined:
        return None
    names = [h["name"] for h in defined if isinstance(h, dict) and "name" in h]
    leaves = _leaf_chains_with_text(role_dir)
    if len(names) != len(defined) or leaves is None:
        return None
    notified = [(_notified_names(json.loads(text)), chain) for text, chain in leaves]
    chains: list[tuple] = []
    for name in names:
        consumers = [chain for notifies, chain in notified if name in notifies]
        if not consumers:
            return None
        chains.extend(consumers)
    return chains


def task_file_chains(role_dir: Path, path: str, pr_range: str, repo: Path):
    """The chains on the leaf tasks of `path` that `pr_range` changed, else on all of them.

    Narrows only when every changed task is found in the checkout's copy: a checkout on
    another commit than the range's end would otherwise match some changed tasks and not
    others, and read narrower than the truth. A miss is printed on stderr, because a
    checkout that predates the merge misses every changed task and the wide note it then
    prints is the #3976 symptom, which must not pass for a narrowed answer.
    """
    task_file = Path(path).name
    every = task_chains(role_dir, lambda task, f: f == task_file)
    changed = _changed_tasks(path, pr_range, repo) if pr_range else None
    if not changed:
        return every
    matched: set[str] = set()

    def keep(task, f) -> bool:
        text = json.dumps(task)
        if f == task_file and text in changed:
            matched.add(text)
            return True
        return False

    chains = task_chains(role_dir, keep)
    if matched == changed:
        return chains
    old, _, new = pr_range.partition("..")
    _warn(
        f"the task chains read from {role_dir} reach {len(matched)} of the "
        f"{len(changed)} tasks {old[:8]}..{new[:8]} changed in {path}, so its owed-host "
        "note keeps the whole file's reach"
    )
    return every


def _warn(message: str) -> None:
    """One line on stderr, where `land.sh`'s operator and journal both see it."""
    print(f"land_reach: {message}", file=sys.stderr)


def deleted_in(path: str, pr_range: str, repo: Path) -> bool:
    """Whether `path` exists at the start of `pr_range` and not at its end."""
    old, _, new = pr_range.partition("..")
    exists = [
        git("cat-file", "-e", f"{ref}:{path}", cwd=repo, check=False).returncode == 0
        for ref in (old, new)
    ]
    return exists == [True, False]


def _changed_tasks(path: str, pr_range: str, repo: Path) -> set[str] | None:
    """The leaf tasks of `path` that `pr_range` added or edited, or None to stay wide.

    Read from git at both ends of the range, not from the checkout, so a checkout on another
    commit cannot shift the diff. A file absent at either end is None: an added file has no
    narrower reach than its own, and a deleted one ships nothing.
    """
    old, _, new = pr_range.partition("..")
    texts = []
    for ref in (old, new):
        shown = git("show", f"{ref}:{path}", cwd=repo, check=False)
        if shown.returncode != 0:
            return None
        texts.append(shown.stdout)
    return changed_task_texts(*texts)


def changed_task_texts(old_text: str, new_text: str) -> set[str] | None:
    """The leaf tasks of one task file that a change added or edited, as `json.dumps` text.

    A `tasks/` file mixes gated and ungated tasks: `initial_setup/tasks/crons.yml` holds a
    weekly apt cron every host runs beside box-only crons. Its file-level reach is all three
    hosts, so a change to only the box-only crons named an apply on daniel-server and daniel-pi
    that renders nothing there (#3976). The texts returned here let `land_reach` read the gates
    on the changed tasks alone.

    The leaves are `_gates_in`'s: a cross-role import counts as one, and a literal in-role
    import does not. Returns None, and the caller keeps the file-level reach, on any doubt:
    either side unparseable; a block or import whose own keys changed, since its `when:`
    reaches every task under it; a removed task with no edited task of the same `name:` and
    `when:` in its place; or no changed leaf at all.
    """
    try:
        old = yaml_fast.safe_load(old_text) or []
        new = yaml_fast.safe_load(new_text) or []
    except yaml.YAMLError:
        return None
    if not (isinstance(old, list) and isinstance(new, list)):
        return None
    old_leaves, old_frames = _split_leaves(old)
    new_leaves, new_frames = _split_leaves(new)
    if sorted(old_frames) != sorted(new_frames):
        return None
    added = list(new_leaves)
    for text in old_leaves:
        if text in added:
            added.remove(text)
    removed = list(old_leaves)
    for text in new_leaves:
        if text in removed:
            removed.remove(text)
    edited = {_name_and_gate(text) for text in added}
    if not added or any(_name_and_gate(text) not in edited for text in removed):
        return None
    return set(added)


def _name_and_gate(text: str) -> tuple[str, str]:
    task = json.loads(text)
    return json.dumps(task.get("name")), json.dumps(task.get("when"))


def _split_leaves(tasks) -> tuple[list[str], list[str]]:
    """`tasks`' leaf texts, and the text of every block and literal import above them.

    A frame is recorded without its `block:` body, so an edit to a task inside the block
    changes a leaf and not the frame.
    """
    leaves: list[str] = []
    frames: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        target = next((task[k] for k in _IMPORT_KEYS if k in task), None)
        if isinstance(target, str) and "{{" not in target:
            frames.append(json.dumps(task, sort_keys=True))
        elif "block" in task:
            frames.append(json.dumps({k: v for k, v in task.items() if k != "block"}))
            sub_leaves, sub_frames = _split_leaves(task["block"] or [])
            leaves += sub_leaves
            frames += sub_frames
        else:
            leaves.append(json.dumps(task))
    return leaves, frames


@contextlib.contextmanager
def tree_at(ref: str, repo: Path) -> Generator[Path | None]:
    """A temporary copy of `ref`'s `ansible/` tree, or None when git cannot write one.

    The checkout `land.sh` runs from can predate the merge: deploy-ui runs it from the primary
    checkout, which sits at the pre-merge HEAD until the tick fast-forwards. Task chains read
    from that tree are the old ones, so a changed task is never found there and the note fell
    back to the whole file's reach, the #3976 symptom. Reading the merge commit's tree is what
    `classify.adopt_cross_role_tables` does for the cross-role tables.
    """
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ansible.tar"
        root = Path(tmp) / "tree"
        try:
            git("archive", "-o", str(archive), ref, "ansible", cwd=repo)
            with tarfile.open(archive) as tar:
                tar.extractall(root, filter="data")
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            _warn(
                f"could not read {ref[:8]}'s ansible/ tree ({type(exc).__name__}), so the "
                f"owed-host note reads {repo}'s checkout, which can predate it"
            )
            root = None
        yield root
