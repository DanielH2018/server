#!/usr/bin/env python3
"""Refuse a PR whose change spans both deploy planes along a dependency edge (D4.4).

Until D5 ends, an edge between a flux-owned role and an Ansible-owned role has no ordering
mechanism. `land.sh` applies the Ansible half after the merge, while `promote` applies the Flux
half after CI, so a PR that changes both halves lands them in whichever order the two pipelines
happen to finish. This gate renders the cluster at the PR's base and head with `homelab-render
cluster` and refuses two shapes:

- **Arm 1, plane order.** Two counted tags sit on different planes, and one is in the other's
  transitive `build_k8s_dep_map` set, `K8S_APPLIES_LAST` included. A tag counts when its
  `cluster/<tag>/` or `cluster/<tag>-deferred/` output moved, unless its role's own diff is
  only an image pin (`deploy_k8s.py:is_image_only_diff`). A tag whose role's `tasks/` changed
  counts as well when it is Ansible-owned at the head, because a task change moves no render.
  An Ansible-owned tag counts only when the PR changes something under `ansible/`: Ansible
  applies its own templates, so a renderer-only change moves nothing it applies (#5213).
  A flux-owned role's tasks run nowhere, since every Ansible path skips the tag, so only the
  render they feed reaches a plane (D4 Open question 9). An inventory field another role reads
  counts only through the render it moves: an `auth_tier` edit on bento-pdf moves authelia's
  access rules alone, so no pair forms.
- **Arm 2, edge first.** A tag that is flux-owned at both ends gains a `dependsOn` edge, and
  its own render moves in the same PR. A tag that becomes flux-owned in the PR is not judged:
  its Kustomization is born with its edges, and D3's `checksum/restart` stamp moves its render
  in the same commit, so judging it would refuse every flip.

There is no bypass. Two PRs that merge back to back each pass alone, and an image bump that does
need ordering passes too; `docs/landing.md` states the `settled` rule that covers both, and how
to split a refused PR. The plan is docs/target-architecture/deploy/d4-ordering.md, *The
cross-plane gate*.

`scripts/dev/cross_plane_replay.py` replays this gate's decision over past master commits
(Open question 9).

Exit codes: 0 clean; 1 refused; 2 a render failed, so the gate could not judge.

Run: uv run python scripts/validate/cross_plane_order.py --base origin/master
     uv run pytest scripts/validate/tests/test_validate_cross_plane_order.py
"""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.git import git, git_env, git_stdout
from lib.repo_paths import FILTER_PLUGINS, GITOPS_DEPLOY_FILES, REPO
from renderer.renderer_lib.cluster import NAMESPACE_ROLE_VARS
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    Ordering,
    kustomization_edges,
)

# A filter plugin and the deployer's library are imported by file path, not as packages.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))
if str(GITOPS_DEPLOY_FILES) not in _sys.path:
    _sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_k8s import is_image_only_diff
from toposort import K8S_APPLIES_LAST, build_k8s_dep_map, filter_by_platform

CLEAN, REFUSED, UNJUDGED = 0, 1, 2
HOST_VARS = Path("ansible/inventory/host_vars/daniel-box.yml")
GROUP_VARS = Path("ansible/inventory/group_vars/all.yml")
ANSIBLE_TREE = "ansible/"
K8S_ROLES = "ansible/roles/k8s/"
SHARED_TEMPLATES = "ansible/templates/"
ANSIBLE_PLANE = "ansible"


class RenderFailed(RuntimeError):
    """`homelab-render cluster` exited non-zero, so the gate has nothing to judge."""


@dataclass(frozen=True)
class Snapshot:
    """What the gate reads from one side of a PR.

    Attributes:
        renders: Each tag mapped to a digest of its `cluster/<tag>/` and
            `cluster/<tag>-deferred/` output. A tag the render left out is absent.
        owners: Each k8s entry mapped to its plane: its `owner` value, or `ansible`.
        deps: Each k8s entry mapped to its `build_k8s_dep_map` edges.
        deferred: Each `<tag>-deferred` directory mapped to its files, for the namespace
            edges `kustomization_edges` derives.
        namespace_roles: Each namespace a role creates mapped to that role.
    """

    renders: Mapping[str, str]
    owners: Mapping[str, str]
    deps: Mapping[str, Iterable[str]]
    deferred: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    namespace_roles: Mapping[str, str] = field(default_factory=dict)

    def plane(self, tag: str) -> str:
        return self.owners.get(tag, ANSIBLE_PLANE)

    def flux_owned(self) -> frozenset[str]:
        return frozenset(t for t, p in self.owners.items() if p == "flux")

    def depends_on(self, tag: str) -> frozenset[str]:
        """The `dependsOn` names the renderer writes for `tag` and its deferred apply."""
        owned = self.flux_owned()
        ordering = Ordering(
            deps=self.deps,
            applies_last=frozenset(K8S_APPLIES_LAST),
            namespace_roles=self.namespace_roles,
            flux_owned=owned,
        )
        directories = {name: {} for name in self.renders} | dict(self.deferred)
        edges = kustomization_edges(owned, directories, ordering)
        return frozenset().union(
            *(edges[n].kept for n in (tag, tag + DEFERRED_SUFFIX) if n in edges)
        )


def closure(deps: Mapping[str, Iterable[str]], tag: str) -> set[str]:
    """Every name `tag` depends on, directly or through another name."""
    seen: set[str] = set()
    stack = list(deps.get(tag, ()))
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(deps.get(name, ()))
    return seen - {tag}


def counted(
    base: Snapshot,
    head: Snapshot,
    changed_paths: Iterable[str],
    role_diffs: Mapping[str, str],
) -> set[str]:
    """The tags the PR changes, by render, by an Ansible-owned `tasks/`, never by a pin alone.

    An Ansible-owned tag counts only when a changed path is under `ansible/`.

    Args:
        changed_paths: The paths the PR changes.
        role_diffs: Each tag mapped to the unified diff of its role directory.
    """
    moved = {
        tag
        for tag in base.renders.keys() | head.renders.keys()
        if base.renders.get(tag) != head.renders.get(tag)
    }
    changed_paths = list(changed_paths)
    # A role's image-pin diff exempts it only when nothing outside the roles moved too: a
    # shared template or an inventory edit beside the bump also moves the role's render.
    if all(p.startswith(K8S_ROLES) for p in changed_paths):
        moved -= {tag for tag in moved if is_image_only_diff(role_diffs.get(tag, ""))}
    # DECIDED: an Ansible-owned tag's render move counts only when the PR changes something
    # under `ansible/`. Ansible applies its own templates, never `homelab-render` output, so a
    # renderer-only PR moves nothing Ansible applies; only the flux-owned half reaches a
    # cluster, through `promote` (#5213).
    if not any(p.startswith(ANSIBLE_TREE) for p in changed_paths):
        moved = {tag for tag in moved if head.plane(tag) != ANSIBLE_PLANE}
    tasks = {
        p[len(K8S_ROLES) :].split("/", 1)[0]
        for p in changed_paths
        if p.startswith(K8S_ROLES) and "/tasks/" in p
    }
    # DECIDED: a `tasks/` change counts only for a tag Ansible owns at the head. The tasks of a
    # flux-owned role run nowhere, and the render they feed is already in `moved`. Counting them
    # refused three mechanical edits in the 30-day replay that no plane could misorder. The
    # operator ruled on 2026-10-16 (D4 Open question 9).
    tasks &= base.owners.keys() | head.owners.keys()
    return moved | {tag for tag in tasks if head.plane(tag) == ANSIBLE_PLANE}


def problems(
    base: Snapshot,
    head: Snapshot,
    changed_paths: Iterable[str],
    role_diffs: Mapping[str, str],
) -> list[str]:
    """Every reason the PR must be split, one line each; empty when it may land as one."""
    changed_paths = list(changed_paths)
    tags = sorted(counted(base, head, changed_paths, role_diffs))
    found = []
    for dependent in tags:
        needs = closure(head.deps, dependent)
        for dependency in tags:
            if dependency in needs and head.plane(dependency) != head.plane(dependent):
                found.append(
                    f"arm 1: {dependent} ({head.plane(dependent)}) depends on {dependency} "
                    f"({head.plane(dependency)}), and this PR changes both. Land "
                    f"{dependency}'s half first, and merge {dependent}'s only after that "
                    f"PR's land.sh verdict reads `settled`. A shared-template change splits "
                    f"by copy and migrate (docs/landing.md)."
                )
    for tag in tags:
        if tag not in base.flux_owned() or tag not in head.flux_owned():
            continue
        gained = head.depends_on(tag) - base.depends_on(tag)
        if gained and base.renders.get(tag) != head.renders.get(tag):
            found.append(
                f"arm 2: {tag} gains dependsOn {sorted(gained)} and its own render moves "
                f"in the same PR. Land the edge alone first, then the change."
            )
    return found


def _digest(files: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name].encode() + b"\0")
    return h.hexdigest()


def _read_dir(path: Path) -> dict[str, str]:
    if not path.is_dir():
        return {}
    return {
        str(p.relative_to(path)): p.read_text()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def snapshot(
    tree: Path, roles: Iterable[str] = (), python: str = sys.executable
) -> Snapshot:
    """Render `tree` with its own renderer and read what the gate judges.

    The render runs `tree`'s `homelab_render.py` under `python`, so each side of a PR renders
    with its own code. The dependency map is this checkout's `build_k8s_dep_map` over `tree`'s
    inventory and templates.

    Args:
        roles: The tags to render, or none for every render target.

    Raises:
        RenderFailed: The render exited non-zero.
    """
    entries = yaml_fast.safe_load((tree / HOST_VARS).read_text())
    k8s = filter_by_platform(entries.get("containers_list") or [], "k8s")
    names = {e["name"] for e in k8s}
    with tempfile.TemporaryDirectory(prefix="cross-plane-") as out:
        # DECIDED: both sides run under the PR's interpreter, not each commit's own `uv run
        # --frozen` environment. A dependency bump that changes the render is then invisible
        # to the gate, but no plane orders on a lockfile, and a second environment per run
        # costs a sync. D4 Open question 12 leaves a shared layout with D6.1 to D6.
        cmd = [python, str(tree / "scripts/renderer/homelab_render.py"), "cluster"]
        cmd += ["--out", out] + [arg for r in roles for arg in ("--role", r)]
        proc = subprocess.run(
            cmd, cwd=tree, capture_output=True, text=True, env=git_env(), timeout=900
        )
        if proc.returncode:
            tail = "\n".join(proc.stderr.splitlines()[-5:])
            raise RenderFailed(
                f"homelab-render exited {proc.returncode} in {tree}:\n{tail}"
            )
        cluster = Path(out) / "cluster"
        dirs = {p.name: _read_dir(p) for p in cluster.iterdir() if p.is_dir()}
    renders = {
        tag: _digest(
            {f"{tag}/{k}": v for k, v in dirs[tag].items()}
            | {
                f"deferred/{k}": v
                for k, v in dirs.get(tag + DEFERRED_SUFFIX, {}).items()
            }
        )
        for tag in names
        if tag in dirs
    }
    group = yaml_fast.safe_load((tree / GROUP_VARS).read_text())
    return Snapshot(
        renders=renders,
        owners={e["name"]: str(e.get("owner") or ANSIBLE_PLANE) for e in k8s},
        deps=build_k8s_dep_map(k8s, str(tree / "ansible")),
        deferred={n: f for n, f in dirs.items() if n.endswith(DEFERRED_SUFFIX)},
        namespace_roles={
            str(group[var]): role
            for var, role in NAMESPACE_ROLE_VARS.items()
            if var in group
        },
    )


def extract(sha: str, dest: Path, paths: Iterable[str] = (), repo: Path = REPO) -> None:
    """Unpack `git archive` of `sha` in `repo`, narrowed to `paths` when given, under `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cross-plane-tar-") as tmp:
        tar_path = Path(tmp) / "tree.tar"
        git("archive", "--format=tar", "-o", str(tar_path), sha, "--", *paths, cwd=repo)
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter="data")


def changes(base: str, head: str) -> tuple[list[str], dict[str, str]]:
    """The paths `base..head` changes, and each k8s role's unified diff."""
    paths = git_stdout(
        "diff", "--no-renames", "--name-only", base, head, cwd=REPO
    ).split()
    roles = {
        p[len(K8S_ROLES) :].split("/", 1)[0] for p in paths if p.startswith(K8S_ROLES)
    }
    diffs = {
        role: git(
            "diff", "--no-renames", base, head, "--", K8S_ROLES + role, cwd=REPO
        ).stdout
        for role in roles
    }
    return paths, diffs


def gate(base: str, head: str) -> int:
    """Judge `base..head`, each side rendered with its own tree's code."""
    base, head = (
        git_stdout("rev-parse", "--verify", f"{r}^{{commit}}", cwd=REPO)
        for r in (base, head)
    )
    paths, diffs = changes(base, head)
    with tempfile.TemporaryDirectory(prefix="cross-plane-trees-") as tmp:
        snaps = []
        for name, sha in (("base", base), ("head", head)):
            extract(sha, Path(tmp) / name, ("ansible", "scripts"))
            try:
                snaps.append(snapshot(Path(tmp) / name))
            except RenderFailed as exc:
                print(
                    f"cross-plane-order: cannot judge, the {name} render failed. {exc}",
                    file=sys.stderr,
                )
                return UNJUDGED
    found = problems(snaps[0], snaps[1], paths, diffs)
    for line in found:
        print(f"[FAIL] {line}", file=sys.stderr)
    if not found:
        print(
            f"cross-plane-order: {base[:9]}..{head[:9]} changes no pair across the planes"
        )
    return REFUSED if found else CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the PR's base commit")
    parser.add_argument(
        "--head", default="HEAD", help="the PR's head commit (default HEAD)"
    )
    args = parser.parse_args(argv)
    return gate(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
