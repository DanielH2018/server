#!/usr/bin/env python3
"""Which checkout the health gate renders the deployed role's manifests from.

`probe.py health <tag>` enumerates the workloads it must gate by rendering the role's
manifests, from the checkout the probe.py it ran came from. A landing that deployed a
snapshot of its merge commit has not moved the primary checkout at
all, so gating from there enumerates nothing for a role that commit ADDS and the whole gate
reads `skipped` -- green, on the first landing of a new service.

Its own module rather than beside the health gate's other tests, because it asks a question
of its own.

Run: uv run pytest scripts/deploy_tools/tests/test_gate_render_source.py
"""

import types

from deploy_tools.land_lib import health_gate as notify_mod


def _result(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _k8s_tools(run):
    return notify_mod.GateTools(run=run, tag_platforms=lambda tag, **_: {"k8s"})


def _pi_inventory(root, tag):
    """Write a checkout whose ONLY containers_list entry is `tag`, declared on the Pi."""
    host_vars = root / "ansible" / "inventory" / "host_vars"
    host_vars.mkdir(parents=True)
    (host_vars / "daniel-pi.yml").write_text(f"containers_list:\n  - name: {tag}\n")


def test_the_gate_renders_from_the_checkout_it_is_pointed_at(tmp_path):
    """A landing gates the snapshot it deployed, not the checkout land.py lives in.

    The second assertion is the load-bearing one: `probe.py` is named RELATIVELY, which is the
    entire mechanism by which `cwd` moves the render root. An absolute path would make `cwd`
    decorative and the gate would still report `ok` from the wrong tree.
    """
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs.get("cwd")))
        return _result(0, "sonarr: 1/1 ready, 1 updated, restarts=0")

    ok, _lines, *_ = notify_mod.gate(["sonarr"], tools=_k8s_tools(run), cwd=tmp_path)
    argv, cwd = seen[0]
    assert ok is True
    assert cwd == tmp_path
    assert "scripts/diagnostics/probe.py" in argv
    assert not any(str(arg).startswith("/") for arg in argv)


def test_without_a_cwd_the_gate_renders_this_checkout(tmp_path):
    """CLEAN half: a caller that names no checkout gets the one this file lives in."""
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs.get("cwd"))
        return _result(0, "sonarr: 1/1 ready, 1 updated, restarts=0")

    notify_mod.gate(["sonarr"], tools=_k8s_tools(run))
    assert seen == [notify_mod.REPO]


def test_the_platform_routing_reads_the_tree_the_probe_renders(tmp_path):
    """A tag declared only at the deployed commit routes to the Pi, not k8s-first.

    The manifests came from the snapshot while `tag_platforms` read the calling checkout's
    inventory, so the two halves of one verdict came from two trees. For a PR that adds a Pi
    role and its `containers_list` entry together, the calling tree answers `set()`, which falls
    to the last branch of `check_one` and probes the CLUSTER first -- where a same-named
    workload answers 0 for a Pi container nobody deployed.

    Driven through the real `tag_platforms`: a stub would assert nothing about which inventory
    was read.
    """
    tag = "pi-only-at-the-snapshot"
    _pi_inventory(tmp_path, tag)
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return _result(0, f"{tag}: up")

    state, _detail = notify_mod.check_one(
        tag, tools=notify_mod.GateTools(run=run), cwd=tmp_path
    )
    assert state == "ok"
    assert [("--docker" in argv) for argv in seen] == [True]


def test_a_tag_no_tree_declares_still_probes_k8s_first(tmp_path):
    """CLEAN half: the fallback order is unchanged for a tag the rendered tree does not declare.

    Same call with an inventory that declares nothing -- `set()` keeps the old order, k8s then
    `--docker`, which is what a block tag reaching the gate relies on.
    """
    (tmp_path / "ansible" / "inventory" / "host_vars").mkdir(parents=True)
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return _result(0, "config: up")

    notify_mod.check_one("config", tools=notify_mod.GateTools(run=run), cwd=tmp_path)
    assert [("--docker" in argv) for argv in seen] == [False]
