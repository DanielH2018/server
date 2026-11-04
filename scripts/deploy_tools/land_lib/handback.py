"""The refusal to merge a PR that hands a tag back to Ansible while Flux still owns it.

`merge.await_merge` runs `refuse_handback_while_flux_owns` before the direct merge. The reads it
makes are the `Tools.flux` boundaries in `land_flux.py`.
"""

import subprocess

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say


def refuse_handback_while_flux_owns(ln: Landing, head: str) -> None:
    """Die before the merge while Flux still owns a tag this PR makes Ansible-owned.

    A handback is two PRs (docs/flux-deploy-plane.md, *Handing a service back*): the first
    sets `owner: released`, the root prunes the tag's Kustomization, and only then does the
    second remove `owner`. Merged while the Kustomization exists, the second hands the tag to
    Ansible while Flux still reconciles it, so two writers apply it. This compares `tag_owners`
    at the PR's merge base and at `head`, and for each tag that loses `owner` and keeps a k8s
    entry at `head`, reads the Kustomization with the read-only kubectl. Only `NotFound` lets
    the merge proceed. A read that fails refuses too, since it cannot rule the second writer
    out.

    A tag whose entry is gone at `head` is a retirement's second PR (docs/flux-deploy-plane.md,
    *Retiring a flux-owned service*). No Ansible path applies a tag with no entry, and the root
    prunes its Kustomization only after that PR merges, so the Kustomization goes unread.

    The guard covers only a merge `land.sh` makes. A merge made elsewhere bypasses it, and the
    PR body's quoted `NotFound` is then the only guard (d2-first-leaf.md, *Rollback*).
    """
    pr = ln.opts.pr
    ln.git(
        "fetch", "-q", "--no-write-fetch-head", "origin", f"refs/pull/{pr}/head", BRANCH
    )
    base = ln.git("merge-base", f"origin/{BRANCH}", head)
    if base.returncode != 0 or not base.stdout.strip():
        ln.die(
            f"could not find PR #{pr}'s merge base with {BRANCH}, so whether it hands a "
            "tag back from Flux is unknown — not merging",
            1,
        )
    try:
        before = ln.tools.flux.owners_at(ln.opts.primary, base.stdout.strip())
        after = ln.tools.flux.owners_at(ln.opts.primary, head)
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        ln.die(
            f"could not read which tags Flux applies at PR #{pr}'s base or head, so "
            f"whether it hands a tag back is unknown — not merging: {exc}",
            1,
        )
    dropped = sorted(before.keys() - after.keys())
    if not dropped:
        return
    try:
        declared = ln.tools.flux.k8s_tags_at(ln.opts.primary, head)
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        ln.die(
            f"could not read the k8s entries at PR #{pr}'s head, so whether it hands "
            f"{', '.join(dropped)} back or retires it is unknown — not merging: {exc}",
            1,
        )
    for tag in dropped:
        if tag not in declared:
            say(
                f"PR #{pr} retires `{tag}`: no k8s entry at its head, so no Ansible path "
                "applies it, and its Kustomization goes unread"
            )
            continue
        try:
            present = ln.tools.flux.kustomization_exists(tag)
        except Exception as exc:
            ln.die(
                f"PR #{pr} hands `{tag}` back to Ansible, and the Kustomization `{tag}` "
                f"could not be read — not merging: {exc}",
                1,
            )
        if present:
            ln.die(
                f"PR #{pr} hands `{tag}` back to Ansible while the Kustomization `{tag}` "
                "still exists in flux-system, so Flux and Ansible would both apply it. "
                "Merge the PR that sets `owner: released` first, wait until `kubectl get "
                f"kustomizations.kustomize.toolkit.fluxcd.io -n flux-system {tag}` returns "
                "NotFound, then re-run this",
                1,
            )
        say(f"`{tag}` is handed back to Ansible; its Kustomization is gone (NotFound)")
