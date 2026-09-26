"""Shared scanners for the k8s guard checks — `when:` coverage, read off the role sources.

Two test modules read the same role tree and ask the same question of it: does the guard sit
on the task, or on the include that pulled its whole file in. `test_k8s_dry_run.py` asks it of
the cluster writes and `test_k8s_dry_run_host_writes.py` of the node writes, so the scanner
lives here rather than in either. Each rule's reasoning stays with the function that carries
it; both callers' module docstrings say what they are for.
"""

import re
from pathlib import Path

from _helpers import REPO

K8S_ROLES = REPO / "ansible/roles/k8s"


def _task_chunks(task_file: Path, strip_trailing_comments: bool = False) -> list[str]:
    """Join each task's (possibly multi-line, folded-scalar) body into one string.

    A raw per-line scan can't see `kubectl … exec pod --` and the write verb it pipes to when
    they land on different physical lines of a `cmd: >-` block — which is the normal shape here.
    Splitting on `- name:` (a task boundary in every file in this tree, block-nested or not) and
    joining what follows gives each task one flat string to search, without needing a full YAML
    parse that would miss commands nested under `block:`/`loop:`.

    Whole-line comments are always dropped. `strip_trailing_comments` additionally drops a
    trailing `#…` from every line BEFORE they are joined — per line, because joining first and
    stripping after would delete the rest of the task. That direction is only safe for a check
    that must not credit a comment (the guard search below); it is deliberately off for the
    mutation search, where dropping text could only hide a write.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in task_file.read_text().splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*name:", stripped):
            if current:
                chunks.append(" ".join(current))
            current = []
        if stripped.startswith("#"):
            continue
        if strip_trailing_comments:
            stripped = re.sub(r"\s#.*$", "", stripped)
        current.append(stripped)
    if current:
        chunks.append(" ".join(current))
    return chunks


# Whole-identifier, not a bare substring: janitorr's unrelated `janitorr_dry_run: "{{
# janitorr_k8s_dry_run }}"` contains the literal text "k8s_dry_run" inside a longer variable
# name, which a substring check reads as a guard that isn't there. `\b` anchors this to the
# real fact names, which are underscore-joined identifiers with no boundary in the middle of
# `janitorr_k8s_dry_run` for `\b` to land on.
_GUARD_FACT = re.compile(r"\bk8s_no_mutate\b|\bk8s_dry_run\b")


_INCLUDED_FILE = re.compile(r"(?:import_tasks|include_tasks):\s*[\"']?([\w.-]+\.ya?ml)")


def _guard_covered_files(role: Path) -> set[str]:
    """Task files every one of whose tasks inherits a no-mutation guard from its caller.

    volume-claim is the shape this exists for: main.yml is a single
    `import_tasks: seed.yml` under `when: not k8s_no_mutate`, and the import propagates that
    `when` to every task in seed.yml — and on to copy.yml, which seed.yml includes. Nothing in
    either file names the guard, so a per-task rule alone would call the role unguarded and
    demand it be added to k8s_dry_run_unsupported, where it would do nothing (the refusal reads
    --tags, and volume-claim is reached as a dependency of 25 roles).

    Only main.yml is scanned for the guarded include; from there the closure is transitive and
    unconditional, because a file whose caller is guarded is guarded whatever it does next.
    """
    tasks_dir = role / "tasks"
    main = tasks_dir / "main.yml"
    if not main.is_file():
        return set()
    pending = [
        m.group(1)
        for chunk in _task_chunks(main, strip_trailing_comments=True)
        if _GUARD_FACT.search(chunk)
        for m in [_INCLUDED_FILE.search(chunk)]
        if m
    ]
    covered: set[str] = set()
    while pending:
        name = pending.pop()
        if name in covered:
            continue
        covered.add(name)
        included = tasks_dir / name
        if not included.is_file():
            continue
        for chunk in _task_chunks(included):
            m = _INCLUDED_FILE.search(chunk)
            if m:
                pending.append(m.group(1))
    return covered


def _role_with_tasks(tmp_path: Path, **files: str) -> Path:
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    for name, body in files.items():
        (role / "tasks" / f"{name}.yml").write_text(body)
    return role
