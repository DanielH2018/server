#!/usr/bin/env python3
"""Refuse a PR whose render diff Flux would apply unsafely (D4.4, D5.1d, D5.1f, D6.12).

This gate renders the cluster at the PR's base and head with `homelab-render cluster` and
refuses four shapes. It was `cross_plane_order.py` until D6.12, when every k8s role was
flux-owned and its arm 1, the order between a flux-owned and an Ansible-owned tag along one
dependency edge, lost its subject. The numbers of the arms that stay are kept, because docs,
commit messages and `ansible/deploy.yml` cite them. A tag counts for arm 2 whenever its
`cluster/<tag>/` or `cluster/<tag>-deferred/` output moves; the image-pin exception went with
arm 1 (docs/target-architecture/deploy/d6-delete-old-path.md, *The render-diff gates
after D6*).

- **Arm 2, edge first.** A tag that is flux-owned at both ends gains a `dependsOn` edge, and
  its own render moves in the same PR. A tag that becomes flux-owned in the PR is not judged:
  its Kustomization is born with its edges, and D3's `checksum/restart` stamp moves its render
  in the same commit, so judging it would refuse every flip.
- **Arm 3, a dropped object (D5.1f).** A tag flux-owned at both ends loses an object of any
  kind from its render, and no commit in `base..head` carries `Removes-Object:
  <kind>/<namespace>/<name>` for it (`lib/removes_object.py`). Flux would prune it while its
  Kustomization reads Ready. A rename passes: the object's kind, namespace and name, with the
  renderer's trailing `-<digest8>` removed, still render in the head. So does an object
  labelled or annotated `kustomize.toolkit.fluxcd.io/prune: disabled`, which prune never
  deletes. A handback passes, since a tag that is not flux-owned at the head is not judged.
- **Arm 4, a flux-owned retirement (D5.1f).** A name joins `k8s_retired_services` whose
  entry carries `owner: flux` on the base. The play's teardown deletes the last render
  Ansible staged, and Flux at the old `prod` re-creates it. Such a service retires by two
  PRs instead (docs/flux-deploy-plane.md, *Retiring a flux-owned service*).
- **Arm 5, a suspended tag's render (D5.1d).** A tag that `digests.json` marks `suspended` at
  both ends has a different `own` digest, its `cluster/<tag>/` output alone. A suspended tag
  applies nothing until its resume, so the change would sit merged and unapplied. The same
  change passes in the PR that resumes the tag, since the tag is then not suspended at the head.
  The arm reads the `suspended` field, never the `flux_suspend` key, so a role that derives
  the key from another variable is covered too (d5-bulk-move.md, *A suspended role takes no
  change*). A shared-template change whose render reaches the tag waits for the resume.

Arm 3 reads the trailers from the PR's own commits, and the CI step runs on pull requests
only, where `HEAD^1..HEAD` is those commits and the merge.

There is no bypass for arm 2. Two PRs that merge back to back each pass alone; `docs/landing.md`
states how to split a refused PR. The plan is docs/target-architecture/deploy/d4-ordering.md,
*The cross-plane gate*.

Exit codes: 0 clean; 1 refused; 2 a render failed, so the gate could not judge.

Run: uv run python scripts/validate/render_diff_gates.py --base origin/master
     uv run pytest scripts/validate/tests/test_validate_render_diff_gates.py
"""

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools.changed_set import suspended_changed
from deploy_tools.object_loss import bundle_documents, prune_disabled, stem
from deploy_tools.render_targets import render_targets
from deploy_tools.promote_lib.prune import ReadError
from lib import removes_object, yaml_fast
from lib.cli_help import ArgumentParser
from lib.ansible_inventory import K8S_HOST
from lib.git import git, git_env, git_stdout
from lib.repo_paths import FILTER_PLUGINS, GITOPS_DEPLOY_FILES, REPO
from renderer.renderer_lib.bundle import DIGESTS, SHARED_KINDS
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

from toposort import K8S_APPLIES_LAST, build_k8s_dep_map, filter_by_platform

CLEAN, REFUSED, UNJUDGED = 0, 1, 2
HOST_VARS = Path("ansible/inventory/host_vars/daniel-box.yml")
GROUP_VARS = Path("ansible/inventory/group_vars/all.yml")
K8S_ROLES = "ansible/roles/k8s/"
ANSIBLE_PLANE = "ansible"
FLUX_PLANE = "flux"


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
        objects: Each rendered directory's tag mapped to its objects, as
            `<kind>/<namespace>/<name>`; a `<tag>-deferred` directory's count for `<tag>`.
        unpruned: The objects labelled or annotated `kustomize.toolkit.fluxcd.io/prune:
            disabled`.
        retired: The names `k8s_retired_services` lists.
        digests: The render's parsed `digests.json`, or empty when no tag is flux-owned.
    """

    renders: Mapping[str, str]
    owners: Mapping[str, str]
    deps: Mapping[str, Iterable[str]]
    deferred: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    namespace_roles: Mapping[str, str] = field(default_factory=dict)
    objects: Mapping[str, Iterable[str]] = field(default_factory=dict)
    unpruned: Iterable[str] = ()
    retired: Iterable[str] = ()
    digests: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def plane(self, tag: str) -> str:
        return self.owners.get(tag, ANSIBLE_PLANE)

    def flux_owned(self) -> frozenset[str]:
        return frozenset(t for t, p in self.owners.items() if p == FLUX_PLANE)

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


def counted(base: Snapshot, head: Snapshot) -> set[str]:
    """The tags whose render moves between the two sides, a tag added or dropped included."""
    return {
        tag
        for tag in base.renders.keys() | head.renders.keys()
        if base.renders.get(tag) != head.renders.get(tag)
    }


def problems(base: Snapshot, head: Snapshot) -> list[str]:
    """Arm 2's reasons the PR must be split, one line each; empty when it may land as one."""
    found = []
    for tag in sorted(counted(base, head)):
        if tag not in base.flux_owned() or tag not in head.flux_owned():
            continue
        gained = head.depends_on(tag) - base.depends_on(tag)
        if gained:
            found.append(
                f"arm 2: {tag} gains dependsOn {sorted(gained)} and its own render moves "
                f"in the same PR. Land the edge alone first, then the change."
            )
    return found


def removal_problems(
    base: Snapshot, head: Snapshot, declared: Iterable[str]
) -> list[str]:
    """Arms 3 and 4: what the PR deletes from the cluster without saying so, one line each.

    Args:
        declared: The objects a `Removes-Object` line in the PR's commits declares.
    """
    declared, unpruned = set(declared), set(base.unpruned)
    # Only what Flux applies at the head holds an object, as only that reaches `decide`'s
    # bundle: an object moved to an Ansible-owned tag is still pruned. A shared-kind directory
    # has no entry, so it counts.
    held = set().union(
        *(
            set(idents)
            for tag, idents in head.objects.items()
            if head.owners.get(tag, FLUX_PLANE) == FLUX_PLANE
        )
    )
    held_stems = {stem(ident) for ident in held}
    found = []
    for tag in sorted(base.flux_owned() & head.flux_owned()):
        for ident in sorted(set(base.objects.get(tag, ())) - held):
            if ident in declared or ident in unpruned or stem(ident) in held_stems:
                continue
            found.append(
                f"arm 3: {tag} no longer renders {ident}, so Flux prunes it while its "
                f"Kustomization reads Ready. If the removal is meant, add the line "
                f"`Removes-Object: {ident}` to one of the PR's commits "
                f"(docs/flux-deploy-plane.md, *Removing an object on purpose*)."
            )
    for name in sorted(set(head.retired) - set(base.retired)):
        if base.plane(name) == FLUX_PLANE:
            found.append(
                f"arm 4: {name} joins k8s_retired_services while its base entry carries "
                f"owner: flux, so the play's teardown deletes a stale staged render that Flux "
                f"at the old prod re-creates. Retire it by two PRs: empty its templates, then "
                f"remove its entry (docs/flux-deploy-plane.md, *Retiring a flux-owned "
                f"service*)."
            )
    return found


def suspended_problems(base: Snapshot, head: Snapshot) -> list[str]:
    """Arm 5: each tag suspended at both ends whose own render the PR changes, one line each."""
    return [
        f"arm 5: {tag} is suspended (flux_suspend) at the base and at the head, and this PR "
        f"changes its render. A suspended tag applies nothing until its resume, so the change "
        f"would sit merged and unapplied. Land it in the PR that resumes {tag}, or after it "
        f"(docs/flux-deploy-plane.md, *Stopping one service: `flux_suspend`*)."
        for tag in suspended_changed(base.digests, head.digests)
    ]


def _objects(dirs: Mapping[str, Mapping[str, str]]) -> tuple[dict, frozenset[str]]:
    """Each tag's objects, and the ones prune skips, from `cluster/`'s directories."""
    files = {
        f"cluster/{name}/{path}": text.encode()
        for name, contents in dirs.items()
        for path, text in contents.items()
    }
    objects: dict[str, set[str]] = {}
    unpruned = set()
    try:
        for ident, directory, doc in bundle_documents(files):
            objects.setdefault(directory.removesuffix(DEFERRED_SUFFIX), set()).add(
                ident
            )
            if prune_disabled(doc):
                unpruned.add(ident)
    except ReadError as exc:
        raise RenderFailed(str(exc)) from exc
    return {tag: frozenset(idents) for tag, idents in objects.items()}, frozenset(
        unpruned
    )


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
    tree: Path,
    roles: Iterable[str] = (),
    python: str = sys.executable,
    targets: Callable[[str, Path, Path], list[str]] = render_targets,
) -> Snapshot:
    """Render `tree` with its own renderer and read what the gate judges.

    The render runs `tree`'s `homelab_render.py` under `python`, so each side of a PR renders
    with its own code. The dependency map is this checkout's `build_k8s_dep_map` over `tree`'s
    inventory and templates.

    Every tag is passed with `--role`, so the render writes the publish bundle and its
    `digests.json` for the flux-owned ones. The shared kinds' directories are left out of what
    arms 1 to 3 read, as they were before the bundle was written.

    Args:
        roles: The tags to render, or none for every render target of `tree`.
        targets: How the render targets are found when `roles` is empty, called with the
            host, `tree`'s host_vars directory and its k8s roles directory. A test narrows
            it to keep the no-roles path without rendering every tag.

    Raises:
        RenderFailed: The render exited non-zero.
    """
    entries = yaml_fast.safe_load((tree / HOST_VARS).read_text())
    k8s = filter_by_platform(entries.get("containers_list") or [], "k8s")
    names = {e["name"] for e in k8s}
    roles = list(roles) or targets(K8S_HOST, tree / HOST_VARS.parent, tree / K8S_ROLES)
    with tempfile.TemporaryDirectory(prefix="cross-plane-") as out:
        # DECIDED: both sides run under the PR's interpreter, not each commit's own `uv run
        # --frozen` environment. A dependency bump that changes the render is then invisible
        # to the gate, but no plane orders on a lockfile, and a second environment per run
        # costs a sync. D4 Open question 12 leaves a shared layout with D6.1 to D6.
        cmd = [python, str(tree / "scripts/renderer/homelab_render.py"), "cluster"]
        cmd += ["--out", out]
        cmd += [arg for r in roles for arg in ("--role", r)]
        # The all-zero SHA is soak.NORMALISED_REVISION: R never moves the footprint. It goes in
        # the environment, not a flag, so an older renderer ignores it rather than refusing it.
        env = {**git_env(), "HOMELAB_RENDER_REVISION": "0" * 40}
        proc = subprocess.run(
            cmd, cwd=tree, capture_output=True, text=True, env=env, timeout=900
        )
        if proc.returncode:
            tail = "\n".join(proc.stderr.splitlines()[-5:])
            raise RenderFailed(
                f"homelab-render exited {proc.returncode} in {tree}:\n{tail}"
            )
        cluster = Path(out) / "cluster"
        dirs = {
            p.name: _read_dir(p)
            for p in cluster.iterdir()
            if p.is_dir() and p.name not in SHARED_KINDS
        }
        digests_file = Path(out) / DIGESTS
        try:
            digests = (
                json.loads(digests_file.read_text()) if digests_file.exists() else {}
            )
        except ValueError as exc:
            raise RenderFailed(f"{DIGESTS} is not JSON: {exc}") from exc
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
    objects, unpruned = _objects(dirs)
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
        objects=objects,
        unpruned=unpruned,
        retired=frozenset(
            str(e["name"]) for e in group.get("k8s_retired_services") or []
        ),
        digests=digests if isinstance(digests, dict) else {},
    )


def extract(sha: str, dest: Path, paths: Iterable[str] = (), repo: Path = REPO) -> None:
    """Unpack `git archive` of `sha` in `repo`, narrowed to `paths` when given, under `dest`.

    A path `sha` does not hold is skipped, so a caller can name `hosts` for a commit before it.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if paths := list(paths):
        held = git_stdout("ls-tree", "--name-only", sha, "--", *paths, cwd=repo).split()
        if not (paths := [p for p in paths if p in held]):
            return
    with tempfile.TemporaryDirectory(prefix="cross-plane-tar-") as tmp:
        tar_path = Path(tmp) / "tree.tar"
        git("archive", "--format=tar", "-o", str(tar_path), sha, "--", *paths, cwd=repo)
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter="data")


def render_side(sha: str, dest: Path, repo: Path = REPO) -> Snapshot:
    """`sha`'s tree unpacked under `dest` and rendered with its own renderer."""
    extract(sha, dest, ("ansible", "scripts", "hosts"), repo=repo)
    return snapshot(dest)


def gate(
    base: str,
    head: str,
    repo: Path = REPO,
    render: Callable[[str, Path, Path], Snapshot] = render_side,
) -> int:
    """Judge `base..head`, each side rendered with its own tree's code.

    Args:
        repo: The checkout `base` and `head` name commits in.
        render: Renders one side as `render(sha, dest, repo)`; `render_side` by default.
    """
    base, head = (
        git_stdout("rev-parse", "--verify", f"{r}^{{commit}}", cwd=repo)
        for r in (base, head)
    )
    with tempfile.TemporaryDirectory(prefix="render-diff-trees-") as tmp:
        snaps = []
        for name, sha in (("base", base), ("head", head)):
            try:
                snaps.append(render(sha, Path(tmp) / name, repo))
            except RenderFailed as exc:
                print(
                    f"render-diff-gates: cannot judge, the {name} render failed. {exc}",
                    file=sys.stderr,
                )
                return UNJUDGED
    messages = git_stdout("log", "--format=%B", f"{base}..{head}", cwd=repo)
    found = problems(snaps[0], snaps[1])
    found += removal_problems(snaps[0], snaps[1], removes_object.declared(messages))
    found += suspended_problems(snaps[0], snaps[1])
    for line in found:
        print(f"[FAIL] {line}", file=sys.stderr)
    if not found:
        print(
            f"render-diff-gates: {base[:9]}..{head[:9]} adds no edge beside a change, "
            f"removes nothing Flux owns undeclared and changes no suspended tag"
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
