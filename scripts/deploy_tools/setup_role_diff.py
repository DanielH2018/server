#!/usr/bin/env python3
"""What a PR range changed in a setup role, read from git at both ends of the range.

`land_reach.setup_file_hosts` narrows a changed file's reach to the tasks the range changed
(#3976): a box-only cron edit in `initial_setup/tasks/crons.yml` read as reaching every host
the file's ungated crons run on. Every answer here falls back to the whole file on doubt,
the asymmetry `setup_role_chains` keeps.

Split out of `land_reach.py` at the module-length cap.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.git import git
from setup_role_chains import _IMPORT_KEYS, task_chains


def task_file_chains(role_dir: Path, path: str, pr_range: str, repo: Path):
    """The chains on the leaf tasks of `path` that `pr_range` changed, else on all of them.

    Narrows only when every changed task is found in the checkout's copy: a checkout on
    another commit than the range's end would otherwise match some changed tasks and not
    others, and read narrower than the truth.
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
    return chains if matched == changed else every


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
