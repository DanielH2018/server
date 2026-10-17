#!/usr/bin/env python3
"""Replay the cross-plane gate over the master commits since a date (D4 Open question 9).

D4.4's plan asks how often `scripts/validate/cross_plane_order.py` would have refused a merged
PR. The renderer dates from 2026-10-12, and this checkout's renderer cannot render most older
trees whole: an older role's tasks computed values the renderer no longer derives, and older
inventories name files today's roles no longer ship. So a commit is judged as a transplant:
this checkout's tree with only the render inputs the commit changed set to their content at
its parent and at itself. Ownership is `--flux-owned` at both ends, whatever the commit's own
inventory said. `ansible/vars/secrets.yml` keeps this checkout's copy, because a secret's
value never renders.

A commit that changes `scripts/renderer/` is judged as the CI gate judges it instead: each end
is that commit's own tree, renderer included, so a renderer change that adds a `dependsOn`
edge and moves the render in one commit reads as arm 2 refuses it (#5230). Ownership is still
`--flux-owned` at both ends. Any other `scripts/`-only commit is clean by construction.

A commit whose transplant does not render is judged instead as today's tree against today's
tree with the commit's own change reversed. One that renders neither way is still judged on
the gate's `tasks/` rule, which needs no render, and is otherwise counted as unrenderable,
never as clean.

Every pair across the planes holds a flux-owned tag, and the gate's second arm judges only
those, so the first pass renders the flux-owned tags alone. Only a commit that moves one, or
changes its `tasks/`, gets the full render. `--cache` keeps every render, so a stopped replay
resumes; delete it to render again after the renderer changes.

Run: uv run python scripts/dev/cross_plane_replay.py --since 2026-09-16T00:31:00Z \\
         --flux-owned bento-pdf --jobs 5
     uv run pytest scripts/dev/tests/test_cross_plane_replay.py
"""

import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.git import git, git_stdout
from lib.repo_paths import REPO
from validate.cross_plane_order import (
    HOST_VARS,
    K8S_ROLES,
    SHARED_TEMPLATES,
    RenderFailed,
    Snapshot,
    changes,
    closure,
    counted,
    extract,
    problems,
    snapshot,
)

# What a transplant takes from the replayed commit. Everything else, the renderer and the
# filter plugins included, is this checkout's.
RENDER_INPUTS = (
    "ansible/roles/k8s",
    "ansible/templates",
    "ansible/inventory",
    "ansible/vars",
)
# A commit changing this renders each end with that end's own tree, as the gate does.
RENDERER = "scripts/renderer/"
# A transplant keeps this checkout's copy. A secret's value never renders, only its key name,
# and an older copy still holds keys that later moved to the inventory, `domain` among them,
# which the secret checks then refuse to see rendered.
KEEPS_HEAD = frozenset({"ansible/vars/secrets.yml"})
MAX_STUBS = 20
# The renderer's message for a value nothing defines: `<tag>: '<name>' is undefined`, or
# `<tag>/<file> from <template>: '<name>' is undefined` when a template reads it.
_UNDEFINED = re.compile(r"([\w-]+)(?:/\S+ from \S+)?: '(\w+)' is undefined")
_OWNER_LINE = re.compile(r"^[ \t]+owner:[ \t]*\S+[ \t]*\n", re.MULTILINE)


def set_flux_owned(host_vars: Path, flux_owned: Iterable[str]) -> None:
    """Rewrite `host_vars` so that exactly the entries `flux_owned` names carry `owner: flux`.

    Raises:
        ValueError: `host_vars` has no entry for a name in `flux_owned`.
    """
    text = _OWNER_LINE.sub("", host_vars.read_text())
    for name in flux_owned:
        entry = re.compile(rf"^([ \t]*)- name: {re.escape(name)}[ \t]*\n", re.MULTILINE)
        match = entry.search(text)
        if not match:
            raise ValueError(f"{host_vars} has no entry named {name}")
        indent = match.group(1) + "  "
        text = text[: match.end()] + f"{indent}owner: flux\n" + text[match.end() :]
    host_vars.write_text(text)


def prepare_tree(
    sha: str,
    paths: list[str],
    tree: Path,
    undo: bool = False,
    repo: Path = REPO,
    own: bool = False,
) -> list[str]:
    """Write `repo`'s HEAD under `tree`, with `paths` as `sha` holds them.

    With `own`, `tree` is `sha`'s own `ansible/` and `scripts/` whole instead, renderer
    included, and `paths` is ignored. A path `sha` does not hold is deleted. With `undo`, `paths` keep HEAD's content instead,
    with commit `sha`'s own change to each applied in reverse, file by file. A doc or a test
    renders nothing and is left alone.

    Returns:
        The paths whose reverse patch no longer applies to HEAD; empty for a transplant.
    """
    extract(sha if own else "HEAD", tree, ("ansible", "scripts"), repo)
    if own:
        return []
    if not undo:
        for path in paths:
            (tree / path).unlink(missing_ok=True)
        held = git_stdout("ls-tree", "-r", "--name-only", sha, "--", *paths, cwd=repo)
        if held:
            extract(sha, tree, held.split("\n"), repo)
        return []
    failed = []
    for path in paths:
        if path.endswith(".md") or "/tests/" in path:
            continue
        patch = git("diff", "--binary", sha, f"{sha}^", "--", path, cwd=repo).stdout
        proc = git(
            "apply", "--whitespace=nowarn", "-", cwd=tree, input=patch, check=False
        )
        if patch and proc.returncode:
            failed.append(path)
    return failed


def blockers(
    failed: list[str], tree: Path, snap: Snapshot, owned: Iterable[str]
) -> list[str]:
    """The paths of `failed` whose unreversed change could alter a verdict on `snap`.

    A file today's tree no longer holds is read by nothing. A file under a role that is not
    flux-owned, not on a flux-owned tag's dependency path and not depending on one cannot
    form a pair, so its render moving or not changes no verdict.
    """
    owned = set(owned)
    relevant = set(owned)
    for tag in snap.owners:
        reach = closure(snap.deps, tag)
        if tag in owned:
            relevant |= reach
        elif reach & owned:
            relevant.add(tag)
    found = []
    for path in failed:
        if not (tree / path).exists():
            continue
        role = (
            path[len(K8S_ROLES) :].split("/", 1)[0]
            if path.startswith(K8S_ROLES)
            else None
        )
        if role is None or role in relevant:
            found.append(path)
    return found


def replay_snapshot(
    sha: str,
    paths: list[str],
    flux_owned: list[str],
    workdir: Path,
    roles: Iterable[str] = (),
    undo: bool = False,
    own: bool = False,
) -> Snapshot:
    """`prepare_tree`'s tree rendered with `flux_owned` the only Flux tags.

    An older role's tasks computed some values at deploy time that this checkout's renderer
    no longer derives, `authelia_password_hash` among them. Each one the render reports
    undefined is set to a fixed stub in that role's defaults and the render retried, so both
    ends of a replayed commit render it the same way.

    Raises:
        RenderFailed: The render still fails once no undefined value is left to stub.
    """
    tree = Path(tempfile.mkdtemp(prefix="replay-", dir=workdir))
    try:
        failed = prepare_tree(sha, paths, tree, undo, own=own)
        set_flux_owned(tree / HOST_VARS, flux_owned)
        for _ in range(MAX_STUBS):
            try:
                snap = snapshot(tree, roles)
            except RenderFailed as exc:
                missing = _UNDEFINED.search(str(exc))
                if not missing:
                    raise
                role, var = missing.groups()
                defaults = tree / K8S_ROLES / role / "defaults" / "main.yml"
                defaults.parent.mkdir(parents=True, exist_ok=True)
                with defaults.open("a") as fh:
                    fh.write(f"\n{var}: replay-stub-{var}\n")
                continue
            stuck = blockers(failed, tree, snap, flux_owned)
            if stuck:
                raise RenderFailed(
                    f"{sha}'s change no longer reverses on HEAD in {stuck}"
                )
            return snap
        raise RenderFailed(f"{sha}: more than {MAX_STUBS} undefined values")
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _cached(cache: Path, key: str, render: Callable[[], Snapshot]) -> Snapshot | str:
    """`render()`, or its result from an earlier run's `cache`; a failure's message as a str."""
    path = cache / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return data if isinstance(data, str) else Snapshot(**data)
    try:
        result: Snapshot | str = render()
    except (RenderFailed, ValueError, subprocess.CalledProcessError) as exc:
        result = str(exc)
    data = result if isinstance(result, str) else dataclasses.asdict(result)
    path.write_text(json.dumps(data, default=sorted))
    return result


def replay(since: str, ref: str, flux_owned: list[str], jobs: int, cache: Path) -> int:
    """Run the gate over every first-parent commit on `ref` since `since`; print the tally."""
    cache.mkdir(parents=True, exist_ok=True)
    commits = git_stdout(
        "rev-list", "--first-parent", f"--since={since}", ref, cwd=REPO
    ).split()
    judged = {}
    for sha in commits:
        paths, diffs = changes(f"{sha}^", sha)
        inputs = [
            p
            for p in paths
            if any(p.startswith(i + "/") for i in RENDER_INPUTS) and p not in KEEPS_HEAD
        ]
        own = any(p.startswith(RENDERER) for p in paths)
        if inputs or own:
            judged[sha] = (paths, diffs, inputs, own)
    owned = sorted(flux_owned)

    with tempfile.TemporaryDirectory(prefix="cross-plane-replay-") as tmp:

        def render_all(shas: list[str], roles: list[str]) -> dict[str, Snapshot | str]:
            sides = [(side, sha) for sha in shas for side in (f"{sha}^", sha)]

            def render(item: tuple[str, str]) -> Snapshot | str:
                side, sha = item
                inputs, own = judged[sha][2:]
                how = ["own-tree"] if own else inputs
                key = "|".join([side, *how, "roles", *roles, "flux", *owned])
                return _cached(
                    cache,
                    key,
                    lambda: replay_snapshot(
                        side, inputs, owned, Path(tmp), roles, own=own
                    ),
                )

            with ThreadPoolExecutor(max_workers=jobs) as pool:
                done = list(pool.map(render, sides))
            return {side: snap for (side, _), snap in zip(sides, done, strict=True)}

        def undone(sha: str) -> Snapshot | str:
            inputs = judged[sha][2]
            return _cached(
                cache,
                "|".join(["undo-by-file", sha, *inputs, "flux", *owned]),
                lambda: replay_snapshot(sha, inputs, owned, Path(tmp), undo=True),
            )

        first = render_all(list(judged), owned)
        candidates = []
        for sha, (paths, diffs, _, _) in judged.items():
            base, head = first[f"{sha}^"], first[sha]
            if isinstance(base, str) or isinstance(head, str):
                candidates.append(sha)
            elif counted(base, head, paths, diffs) & set(owned):
                candidates.append(sha)
        print(
            f"replay: {len(candidates)} of {len(judged)} move a flux-owned tag",
            flush=True,
        )
        full = render_all(candidates, [])
        failed = [
            sha
            for sha in candidates
            if isinstance(full[f"{sha}^"], str) or isinstance(full[sha], str)
        ]
        today = _cached(
            cache, "HEAD-tree", lambda: replay_snapshot("HEAD", [], owned, Path(tmp))
        )
        # A commit's own renderer cannot be reversed onto today's tree, so a renderer commit
        # that does not render stays unrenderable.
        undoable = [sha for sha in failed if not judged[sha][3]]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for sha, snap in zip(undoable, pool.map(undone, undoable), strict=True):
                full[f"{sha}^"], full[sha] = snap, today
    refused, shared, by_tasks, unrenderable = [], [], [], []
    # With no render, the `tasks/` rule still judges: both sides take today's dependency map
    # and identical renders, so only a `tasks/` change can count a tag. A lower bound.
    unrendered = (
        Snapshot(renders={}, owners=today.owners, deps=today.deps)
        if not isinstance(today, str)
        else None
    )
    for sha in candidates:
        paths, diffs, _, own = judged[sha]
        base, head = full[f"{sha}^"], full[sha]
        subject = git_stdout("log", "-1", "--format=%s", sha, cwd=REPO)
        if isinstance(base, str) or isinstance(head, str):
            found = problems(unrendered, unrendered, paths, diffs) if unrendered else []
            if found:
                by_tasks.append(sha)
                print(f"REFUSED {sha[:9]} (tasks/ only, unrenderable) {subject}")
                for line in found:
                    print(f"    {line}")
                continue
            unrenderable.append(sha)
            failure = base if isinstance(base, str) else head
            reason = str(failure).strip().splitlines()[-1]
            print(f"UNRENDERABLE {sha[:9]} {subject}\n    {reason}")
            continue
        found = problems(base, head, paths, diffs)
        if found:
            refused.append(sha)
            template = any(p.startswith(SHARED_TEMPLATES) for p in paths)
            if template:
                shared.append(sha)
            way = (
                "own renderer"
                if own
                else "reversed"
                if sha in failed
                else "transplanted"
            )
            tag = " shared-template" if template else ""
            print(f"REFUSED {sha[:9]} ({way}){tag} {subject}")
            for line in found:
                print(f"    {line}")
    reversed_ok = sum(
        not isinstance(full[f"{sha}^"], str) and not isinstance(full[sha], str)
        for sha in undoable
    )
    print(
        f"replay: {len(commits)} commits on {ref} since {since}; {len(judged)} change a render "
        f"input or the renderer and were judged, the rest are clean by construction; {len(failed)} did not "
        f"render as judged, and {reversed_ok} of those rendered "
        f"reversed on today's tree; {len(refused)} refused by render, {len(shared)} of them "
        f"touching a shared template; {len(by_tasks)} more refused on tasks/ alone though "
        f"unrenderable; {len(unrenderable)} unrenderable and not refused on tasks/"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--since", required=True, help="replay the commits since this date"
    )
    parser.add_argument("--ref", default="origin/master", help="the branch to walk")
    parser.add_argument(
        "--flux-owned", action="append", required=True, help="a tag Flux applies"
    )
    parser.add_argument("--jobs", type=int, default=4, help="parallel renders")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cross-plane-replay-cache",
        help="where each render is kept, so a stopped replay resumes",
    )
    args = parser.parse_args(argv)
    return replay(args.since, args.ref, args.flux_owned, args.jobs, args.cache)


if __name__ == "__main__":
    sys.exit(main())
