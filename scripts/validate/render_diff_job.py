#!/usr/bin/env python3
"""Render a PR and its merge base with one commit value, and judge the difference (R4).

The `render-diff` job in `.github/workflows/render-diff.yml` calls this twice. It is not
`render_diff_gates.py`, whose arms refuse an unsafe edge or removal: this job proves that R4's
refactors moved no rendered byte (docs/homelab-render.md, *The render-diff gate*).

- `render` renders one checkout the way the publish job does, with its own `uv` environment,
  its own `published-tags` list and `--revision`.
- `compare` compares two such trees, reads the `Render-diff:` line from `$PR_BODY` and prints
  every problem. `Render-diff: none` allows no difference, and a list of paths must equal the
  differing files. Without the line the job prints the difference and passes, unless the PR
  touches a hazard path. Both trees must also clear a floor, so two empty renders cannot pass.

Exit codes: 0 clean; 1 refused, or a render failed so the job could not judge.

Run: uv run python scripts/validate/render_diff_job.py render --tree . --revision SHA --out DIR
     PR_BODY='Render-diff: none' uv run python scripts/validate/render_diff_job.py compare \
         BASE_DIR HEAD_DIR --merge-base SHA --head SHA
     uv run pytest scripts/validate/tests/test_validate_render_diff_job.py
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.git import git_stdout

# The `flux/` files of the render at #7365's base: 66 tags, three shared kinds and the index.
# A PR that retires a tag lowers this in its own diff and lists the tag's flux file.
FLUX_FLOOR = 70
FLOOR_PATHS = ("flux/pihole.yaml", "flux/traefik.yaml", "cluster/pihole-deferred")
# Moves with any file it digests, so a list need not name it. `none` still refuses it.
DERIVED = "digests.json"
FORMS = (
    "Add one line to the PR body: `Render-diff: none` when no rendered byte moves, or "
    "`Render-diff: flux/x.yaml, cluster/x/y.yaml` listing every file that differs."
)
_LINE = re.compile(r"^Render-diff:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HAZARD_PREFIXES = (
    "scripts/validate/render_diff_job.py",
    "scripts/renderer/",
    "scripts/lib/k8s_roles.py",
    "scripts/lib/ansible_jinja_env.py",
    "ansible/filter_plugins/",
)
_ROLE_TASKS = re.compile(r"^ansible/roles/k8s/[^/]+/tasks/")
_K8S_ROLES = "ansible/roles/k8s/"


def declared(body: str) -> frozenset[str] | None:
    """The paths a PR body's `Render-diff:` line names, empty for `none`, None with no line.

    A line inside a code fence or an HTML comment does not count, so a body that quotes the
    form, or leaves the template's comment in place, binds nothing.

    Raises:
        ValueError: The body holds two different lines, or a line that names nothing.
    """
    text = _COMMENT.sub("", _FENCE.sub("", body.replace("\r\n", "\n")))
    lines = set(_LINE.findall(text))
    if not lines:
        return None
    if len(lines) > 1:
        raise ValueError(f"the PR body holds {len(lines)} different Render-diff: lines")
    names = frozenset(filter(None, re.split(r"[,\s`]+", lines.pop())))
    if not names:
        raise ValueError("the Render-diff: line names nothing")
    return frozenset() if names == {"none"} else names


def hazard_paths(changes: Iterable[tuple[str, str]]) -> list[str]:
    """The `(status, path)` changes that make the line mandatory, as their paths."""
    return [
        path
        for status, path in changes
        if path.startswith(_HAZARD_PREFIXES)
        or _ROLE_TASKS.match(path)
        or (status == "D" and path.startswith(_K8S_ROLES))
    ]


def _files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }


def differing(base: Path, head: Path) -> set[str]:
    """The relative path of every file that one tree lacks or that differs byte for byte."""
    ours, theirs = _files(base), _files(head)
    return {
        rel
        for rel in ours.keys() | theirs.keys()
        if rel not in ours
        or rel not in theirs
        or ours[rel].read_bytes() != theirs[rel].read_bytes()
    }


def _flux_tags(root: Path) -> set[str]:
    return {path.name for path in (root / "flux").glob("*.yaml")}


def floor_problems(root: Path, side: str, floor: int = FLUX_FLOOR) -> list[str]:
    """What `root` lacks of the floor, each problem naming `side`."""
    problems = [
        f"{side}: {rel} is missing from the render"
        for rel in FLOOR_PATHS
        if not (root / rel).exists()
    ]
    count = sum(1 for path in (root / "flux").glob("*") if path.is_file())
    if count < floor:
        problems.append(
            f"{side}: flux/ holds {count} files, under the floor of {floor}. A PR that "
            "retires a tag lowers FLUX_FLOOR in scripts/validate/render_diff_job.py and "
            "lists the tag's flux file"
        )
    return problems


@dataclass(frozen=True)
class Verdict:
    """What `judge` found.

    Attributes:
        problems: Every refusal, one line each; empty when the job passes.
        differences: The relative path of every file that differs between the trees.
    """

    problems: list[str]
    differences: set[str]


def _line_problems(paths: frozenset[str], base: Path, head: Path, moved: set[str]):
    if not paths:
        return [f"Render-diff: none, yet {rel} differs" for rel in sorted(moved)]
    unlisted = moved - paths - {DERIVED}
    unmoved = paths - moved - {DERIVED}
    tags = _flux_tags(base) ^ _flux_tags(head)
    return (
        [
            f"{rel} differs, and the Render-diff: line omits it"
            for rel in sorted(unlisted)
        ]
        + [
            f"the Render-diff: line names {rel}, which does not differ"
            for rel in sorted(unmoved)
        ]
        + [
            f"flux/{name} appears or disappears, and the Render-diff: line omits it"
            for name in sorted(tags)
            if f"flux/{name}" not in paths
        ]
    )


def judge(
    base: Path,
    head: Path,
    changes: Iterable[tuple[str, str]],
    body: str,
    floor: int = FLUX_FLOOR,
) -> Verdict:
    """Judge two rendered trees against the PR's changed paths and body."""
    moved = differing(base, head)
    problems = floor_problems(base, "base", floor)
    try:
        paths = declared(body)
    except ValueError as exc:
        problems += floor_problems(head, "head", floor)
        return Verdict([*problems, f"{exc}. {FORMS}"], moved)
    retired = _flux_tags(base) - _flux_tags(head)
    listed = {name for name in retired if f"flux/{name}" in (paths or ())}
    problems += floor_problems(head, "head", floor - len(listed))
    if paths is not None:
        return Verdict(problems + _line_problems(paths, base, head, moved), moved)
    hazards = hazard_paths(changes)
    if hazards:
        problems.append(
            f"this PR touches {len(hazards)} hazard paths, the first {hazards[0]}, and its "
            f"body has no Render-diff: line. {FORMS}"
        )
    return Verdict(problems, moved)


def render_command(tags: list[str], out: Path, revision: str) -> list[str]:
    """The publish job's render of `tags`: every tag by `--role`, or the shared kinds alone."""
    selection = [arg for tag in tags for arg in ("--role", tag)] or ["--shared-only"]
    return [
        *("uv", "run", "--frozen", "python", "scripts/renderer/homelab_render.py"),
        *("cluster", "--out", str(out), "--all-images", "--revision", revision),
        *selection,
    ]


def render(tree: Path, out: Path, revision: str) -> int:
    """Render the checkout `tree` into `out`, with `tree`'s own code, tag list and lockfile."""
    listing = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/deploy_tools/promote.py",
            "published-tags",
        ],
        cwd=tree,
        stdout=subprocess.PIPE,
        text=True,
    )
    if listing.returncode:
        print(f"render-diff: published-tags exited {listing.returncode} in {tree}")
        return 1
    command = render_command(listing.stdout.split(), out.resolve(), revision)
    proc = subprocess.run(command, cwd=tree)
    if proc.returncode:
        print(f"render-diff: homelab-render exited {proc.returncode} in {tree}")
        return 1
    return 0


def changed_paths(merge_base: str, head: str) -> list[tuple[str, str]]:
    """The PR's `(status, path)` changes, with a rename split into its delete and its add."""
    listing = git_stdout("diff", "--name-status", "--no-renames", merge_base, head)
    return [
        (status, path)
        for status, path in (line.split("\t", 1) for line in listing.splitlines())
    ]


def compare(base: Path, head: Path, merge_base: str, head_sha: str, body: str) -> int:
    """Print the verdict on two rendered trees and return the job's exit code."""
    verdict = judge(base, head, changed_paths(merge_base, head_sha), body)
    print(f"render-diff: {len(verdict.differences)} files differ")
    for rel in sorted(verdict.differences):
        print(f"  {rel}")
    for problem in verdict.problems:
        print(f"render-diff: REFUSED: {problem}")
    return 1 if verdict.problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rendering = commands.add_parser("render", help="render one checkout into --out")
    rendering.add_argument("--tree", type=Path, required=True, help="the checkout")
    rendering.add_argument("--out", type=Path, required=True, help="an empty directory")
    rendering.add_argument(
        "--revision", required=True, help="the commit value both renders take"
    )
    comparing = commands.add_parser("compare", help="judge two rendered trees")
    comparing.add_argument("base", type=Path, help="the merge base's render")
    comparing.add_argument("head", type=Path, help="the PR head's render")
    comparing.add_argument("--merge-base", required=True, help="the merge base's SHA")
    comparing.add_argument(
        "--head", dest="head_sha", required=True, help="the head SHA"
    )
    args = parser.parse_args(argv)
    if args.command == "render":
        return render(args.tree, args.out, args.revision)
    return compare(
        args.base,
        args.head,
        args.merge_base,
        args.head_sha,
        os.environ.get("PR_BODY", ""),
    )


if __name__ == "__main__":
    sys.exit(main())
