"""#5744: a flip PR's health gate reads tag ownership from the commit that landed.

`probe.py health` reads `owner` from the checkout it runs in (`releases_retired.tree_owners`),
and only a tree carrying the flip knows Flux applies the tag. Run from a tree before the
flip, it grades `traefik` against Ansible's frozen release record (NOT ROLLED) and looks
`traefik-crds` up by name, which the Pi's `docker inspect` answers with "no such object".

Every boundary is a `_land_fakes.py` fake but the gate. The gate here is the real
`detach_notify.gate`, over a probe subprocess that answers as the real probe did from each
tree on 2026-10-18. The tree is decided by the `cwd` the landing hands the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_land_flip_gate_tree.py
"""

import contextlib
import subprocess
from dataclasses import replace
from pathlib import Path

from _land_fakes import (
    MERGE_SHA,
    PRIMARY,
    Fakes,
    build_classifier,
    build_tools,
    receipt,
)
from deploy_tools.deploy_lib import detach_notify
from deploy_tools.deploy_lib.detach_notify import NotifyTools
from lib.exit_codes import PROBE_UNREADABLE

SNAPSHOTS = Path("/snap")
FLIPPED = ("traefik", "traefik-crds")


def _carries_the_flip(path: Path, f: Fakes) -> bool:
    """Is `path` inside a tree that holds the merge commit, and so the `owner: flux` lines?

    A snapshot of the merge commit does, and so does the primary once it carries the merge.
    Anything else, land.sh's own checkout included, predates the flip.
    """
    trees = [SNAPSHOTS / MERGE_SHA] + ([PRIMARY] if f.merge_applied_rc == 0 else [])
    return any(path == tree or tree in path.parents for tree in trees)


def _probe(f: Fakes, healthy_everywhere: frozenset[str] = frozenset()):
    """`probe.py health <tag>` as it answers from a tree with and without the flip.

    A tag in `healthy_everywhere` reads healthy from every tree, so a test can leave one
    tag alone to decide the verdict.
    """

    def run(argv, cwd: Path, **_kwargs):
        tag, extra = argv[5], argv[6:]
        flipped = _carries_the_flip(cwd, f) or tag in healthy_everywhere
        rc, out, err = 0, f"{tag}: all 1 workload healthy", ""
        if tag == "traefik" and not flipped:
            rc, out = (
                1,
                (
                    "traefik: 1 of 1 workloads FAILED the gate — homelab/traefik\n"
                    "homelab/traefik: 1/1 ready, 1 updated, restarts=0 — NOT ROLLED — the "
                    "apply at 2026-10-18T20:58:06Z queued a restart and the workload carries "
                    "no restartedAt annotation"
                ),
            )
        if tag == "traefik-crds" and flipped:
            out = (
                "traefik-crds: the role declares no rollout-checkable workload (no "
                "Deployment, DaemonSet or StatefulSet in its manifests) — nothing to gate, "
                "reporting a pass"
            )
        if tag == "traefik-crds" and not flipped:
            # Undeclared there, so the k8s probe guesses a workload by the tag's name, misses,
            # and the Pi's `docker inspect` is asked next.
            rc, out = 1, "traefik-crds: no Deployment or DaemonSet in this namespace"
            if extra == ["--docker"]:
                rc, out = PROBE_UNREADABLE, "PROBE-VERDICT: unreadable"
                err = "error: no such object: traefik-crds"
        return subprocess.CompletedProcess(argv, rc, out + "\n", err)

    def tag_platforms(tag, host_vars):
        flipped = _carries_the_flip(Path(host_vars), f) or tag in healthy_everywhere
        if tag == "traefik-crds" and not flipped:
            return set()
        return {"k8s"}

    return NotifyTools(run=run, tag_platforms=tag_platforms)


def _land(
    monkeypatch, capsys, f: Fakes, healthy_everywhere: frozenset[str] = frozenset()
):
    """`land.main` over `f`, its gate the real one over `_probe`; (rc, stdout)."""
    import land

    tools, calls = build_tools(f)
    notify_tools = _probe(f, healthy_everywhere)

    def gate(tags, cwd=None, **_kwargs):
        calls.append(("gate", (tags,), {"cwd": cwd}))
        return detach_notify.gate(tags, True, tools=notify_tools, cwd=cwd)

    @contextlib.contextmanager
    def snapshot(primary, sha):
        calls.append(("snapshot", (primary, sha), {}))
        yield None if f.gate_snapshot is None else SNAPSHOTS / sha

    tools = replace(tools, gate=gate, snapshot=snapshot)
    monkeypatch.setenv("LAND_PRIMARY", str(PRIMARY))
    rc = land.main(["--pr", "999"], tools=tools, classifier=build_classifier(f, calls))
    return rc, capsys.readouterr().out


def _flip(**kwargs: object) -> Fakes:
    """#5720's shape: traefik and traefik-crds flip to `owner: flux` beside an Ansible tag.

    The `host_vars` edit is a deploy-plane change, so the tick is the apply, and the primary
    checkout does not carry the merge commit (`merge_applied_rc` defaults to 1).
    """
    fakes = Fakes(
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "ansible/inventory/host_vars/daniel-box.yml"}],
                "changedFiles": 1,
            }
        },
        derived=(["traefik", "traefik-crds", "artifacts"], "pr"),
        self_applied=True,
        narrowed=",".join(FLIPPED),
        narrowed_rc=0,
        owners={MERGE_SHA: dict.fromkeys(FLIPPED, "flux")},
        kustomizations=set(FLIPPED),
        state=receipt({"ansible/deploy.yml": ["narrowed-to-nothing"]}),
    )
    return replace(fakes, **kwargs)


def test_a_flip_landed_from_a_checkout_before_its_merge_settles(monkeypatch, capsys):
    """Verify-by, first half: healthy workloads, a tree before the flip, still `settled`."""
    rc, out = _land(monkeypatch, capsys, _flip())
    assert "VERDICT: settled" in out, out
    assert rc == 0


def test_a_flux_only_flip_gates_from_the_commit_that_landed(monkeypatch, capsys):
    """No deploy.sh ran, so no `deployed_at` was set: ownership still comes from the merge."""
    rc, out = _land(monkeypatch, capsys, _flip(derived=([], "pr")))
    assert "VERDICT: settled" in out, out
    assert rc == 0


def test_a_crd_only_tag_is_graded_or_not_applicable(monkeypatch, capsys):
    """Verify-by, second half: `traefik-crds` is no `not graded` line and no deferral.

    traefik reads healthy from every tree here, so the CRD-only tag alone decides the verdict.
    """
    rc, out = _land(
        monkeypatch, capsys, _flip(), healthy_everywhere=frozenset({"traefik"})
    )
    assert "traefik-crds: not graded" not in out, out
    assert "VERDICT: settled" in out, out
    assert rc == 0


def test_a_flip_whose_merge_commit_cannot_be_read_is_not_settled(monkeypatch, capsys):
    """As for a deployed landing: no snapshot and a primary without the merge gate nothing.

    Every probe reads healthy here, so only a gate that refuses to answer from a tree
    without the flip keeps this landing from reading `settled`.
    """
    rc, out = _land(
        monkeypatch,
        capsys,
        _flip(gate_snapshot=None),
        healthy_everywhere=frozenset(FLIPPED),
    )
    assert "VERDICT: settled" not in out, out
    assert rc != 0
