"""The build job's tools cache reaches the bootstrap cases, from the arguments to the container.

A broken link here stays green in CI, because `fetch` and `download_tool` fall back to a
download, so each link has its own check.

Run: uv run pytest scripts/validate/tests/test_bootstrap_seed_wiring.py
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from validate import host_package_cases
from validate.validate_lib import bootstrap_case as case


class _CasesWithoutPackages(host_package_cases.Cases):
    def _prepare(self, render, debs):
        pass


def test_the_tools_cache_argument_reaches_the_cases(tmp_path):
    sources = (
        tmp_path
        / "head/packages/homelab-updater/root/etc/apt/sources.list.d/homelab.sources"
    )
    sources.parent.mkdir(parents=True)
    sources.write_text("URIs: http://box.invalid/\n")
    args = SimpleNamespace(
        image="img",
        python="3.12",
        work=tmp_path,
        render=tmp_path,
        debs=tmp_path,
        tools_cache=Path("cache"),
        k3s_download=False,
    )

    assert _CasesWithoutPackages(args).tools_cache == Path("cache").resolve()


def test_stocking_the_kit_s_pins_reads_the_job_s_cache(tmp_path):
    digest = hashlib.sha256(b"binary").hexdigest()
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / digest).write_bytes(b"binary")
    kit = tmp_path / "kit"
    kit.mkdir()
    downloads = []

    def record_download(url, sha256, dest, cache):
        downloads.append((dest.name, cache))

    cases = SimpleNamespace(tools_cache=seed, work=tmp_path / "work")
    case.stock_pins(cases, case.SCRIPT.read_text(), kit, record_download)

    assert (kit / "tools-cache" / digest).read_bytes() == b"binary"
    assert {name for name, _ in downloads} == set(
        case.kit_pins(case.SCRIPT.read_text())
    )
    assert {cache for _, cache in downloads} == {seed}


def test_each_online_case_mounts_the_seed_and_no_offline_case_does(tmp_path):
    green = tmp_path / "bootstrap-kit" / "green"
    green.mkdir(parents=True)
    (green / "k3s").write_text("k3s")
    seed_mount = f"{green / 'tools-cache'}:{case.TOOLS_SEED}:ro"
    cases = SimpleNamespace(image="img", work=tmp_path, python="3.12")
    clones = {"green": tmp_path, "red-depends": tmp_path, "fixture-host": tmp_path}

    planned = case.plan(tmp_path, green)
    seeded = {
        item.host
        for item in planned
        if seed_mount in case.bootstrap_for(cases, item, clones).container.mounts
        and item.kit is None
    }
    offline_seeded = [
        item.name
        for item in planned
        if item.kit is not None
        and seed_mount in case.bootstrap_for(cases, item, clones).container.mounts
    ]

    assert seeded == set(case.script_value(case.SCRIPT.read_text(), "HOSTS").split())
    assert offline_seeded == []
