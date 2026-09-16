#!/usr/bin/env python3
"""Which checkout the health gate renders the deployed role's manifests from.

`probe.py health <tag>` enumerates the workloads it must gate by rendering the role's
manifests, from the checkout the probe.py it ran came from. A landing that deployed a
snapshot of its merge commit (`deploy.sh --at <sha>`) has not moved the primary checkout at
all, so gating from there enumerates nothing for a role that commit ADDS and the whole gate
reads `skipped` -- green, on the first landing of a new service.

Its own module rather than beside the rest of `deploy_detach_notify`'s tests: that file is
already at its allowlisted length, and this asks a question of its own.

Run: uv run pytest scripts/deploy_tools/tests/test_gate_render_source.py
"""

import types

import deploy_detach_notify as notify_mod


def _result(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _k8s_tools(run):
    return notify_mod.NotifyTools(run=run, tag_platforms=lambda tag, **_: {"k8s"})


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

    ok, _lines = notify_mod.gate(
        ["sonarr"], ansible_ok=True, tools=_k8s_tools(run), cwd=tmp_path
    )
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

    notify_mod.gate(["sonarr"], ansible_ok=True, tools=_k8s_tools(run))
    assert seen == [notify_mod.REPO]


def test_the_cwd_flag_reaches_the_probe(tmp_path):
    """deploy.sh's --detach path is the second caller, and it arrives through argv.

    `gate` grew a `cwd` for land.py while this entry point kept defaulting to REPO, so
    `deploy.sh --detach --at <sha>` deployed one commit and gated another tree. Asserted
    through the REAL gate, down to the directory the probe is run in -- patching `gate` itself
    would pass for a main that read the flag and dropped it.
    """
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs.get("cwd"))
        return _result(0, "sonarr: 1/1 ready, 1 updated, restarts=0")

    base = ["--status", "0", "--log", "/tmp/x", "--tags", "sonarr", "--no-post"]
    for extra in ([], ["--cwd", str(tmp_path)]):
        notify_mod.main(base + extra, tools=_k8s_tools(run))
    assert seen == [notify_mod.REPO, tmp_path]


def test_the_platform_routing_reads_the_tree_the_probe_renders(tmp_path):
    """A tag declared only at the deployed commit routes to the Pi, not k8s-first.

    The manifests came from the snapshot while `tag_platforms` read the calling checkout's
    inventory, so the two halves of one verdict came from two trees. For a PR that adds a Pi
    role and its `containers_list` entry together, the calling tree answers `set()`, which falls
    to the last branch of `check_one` and probes the CLUSTER first -- where a same-named
    workload answers 0 for a Pi container nobody deployed. That is issue #929 again.

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
        tag, tools=notify_mod.NotifyTools(run=run), cwd=tmp_path
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

    notify_mod.check_one("config", tools=notify_mod.NotifyTools(run=run), cwd=tmp_path)
    assert [("--docker" in argv) for argv in seen] == [False]
