"""The held Docker engine's container case, without a container (#6590).

H7's *Verification* names a proof that runs in the CI container: a dummy `docker-ce` at version
A is installed and held, the real `homelab-daniel-pi` carries pins for version B, and after a
`homelab-updater` run `docker-ce` is still at A and `homelab_docker_engine_pending` reads 1. Its
red fixture renders the Pi package with `Depends: docker-ce (= B)` and records what apt does
with the hold: the install fails, or the version moves.

The case installs real debs into a systemd container, so it runs only in CI's `host_packages`
job, from `validate_lib/engine_case.py`. These tests cover what needs no container:

  * the dummy engine builds, which `engine_debs(out)` writes with `dpkg-deb` and returns as
    [A, B], B matching the real Pi package's docker-ce pin and A not;
  * the red fixture, which `depends_variant(out, version)` renders as the real Pi package with
    one extra Depends, `docker-ce (= <version>)`, and returns as the rendered directory;
  * `red_outcome(held, after, apt_ok)`, which names what apt did with the hold;
  * `expect_held(held, installed, prom)`, the green assertion over `dpkg-query` and the prom;
  * where the job runs the case, and that CASE_INPUTS names it.

The module does not exist until the fix adds it, so every test reaches it in its body.

Run: uv run pytest scripts/validate/tests/test_engine_case.py
"""

import ast
from fnmatch import fnmatchcase
import importlib
import importlib.util
from pathlib import Path
import subprocess
from types import ModuleType

import pytest
from lib import yaml_fast
from lib.proc_testing import DEFAULT_TIMEOUT
from renderer.renderer_lib import host as host_mode
from validate.validate_lib import host_fixtures as fx
from validate.validate_lib.host_container import CaseFailed

MODULE = "validate.validate_lib.engine_case"
SOURCE = "scripts/validate/validate_lib/engine_case.py"
CASES = "scripts/validate/host_package_cases.py"
CI_WORKFLOW = ".github/workflows/ci.yml"
PINS = "/usr/lib/homelab/docker-engine.pins"
ENGINE = "docker-ce"
# The functions the fix adds to the module, each reached through getattr.
ENGINE_DEBS = "engine_debs"
DEPENDS_VARIANT = "depends_variant"
RED_OUTCOME = "red_outcome"
EXPECT_HELD = "expect_held"
REFUSED = "refused"
MOVED = "moved"
PENDING_1 = 'homelab_docker_engine_pending{host="daniel-pi"} 1\n'
PENDING_0 = 'homelab_docker_engine_pending{host="daniel-pi"} 0\n'
OK_LINE = 'homelab_updater_last_run_ok{host="daniel-pi"} 1\n'


def _module() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, (
        f"{SOURCE} does not exist, so the job runs no held-engine case"
    )
    return importlib.import_module(MODULE)


def _function(name: str):
    module = _module()
    assert hasattr(module, name), f"{SOURCE} defines no {name}"
    return getattr(module, name)


@pytest.fixture(scope="module")
def pi() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _engine_pin(package: host_mode.Package) -> str:
    """The glob the real Pi package's pins file holds for docker-ce."""
    rows = (row.split() for row in package.files[PINS][0].decode().splitlines())
    (pin,) = [row[1] for row in rows if len(row) == 2 and row[0] == ENGINE]
    return pin


def _field(deb: Path, name: str) -> str:
    return subprocess.run(
        ["dpkg-deb", "--field", str(deb), name],
        capture_output=True,
        text=True,
        check=True,
        timeout=DEFAULT_TIMEOUT,
    ).stdout.strip()


def _engine_builds(tmp_path: Path) -> tuple[Path, Path]:
    debs = list(_function(ENGINE_DEBS)(tmp_path / "engine"))
    assert len(debs) == 2, f"{ENGINE_DEBS} built {len(debs)} debs, not A and B"
    return debs[0], debs[1]


# The dummy engine builds.


def test_both_dummy_builds_are_docker_ce_and_b_is_newer_than_a(tmp_path):
    a, b = _engine_builds(tmp_path)
    assert [_field(deb, "Package") for deb in (a, b)] == [ENGINE, ENGINE]
    assert fx.newer(fx.deb_version(b), fx.deb_version(a))


def test_b_matches_the_pi_packages_docker_ce_pin_and_a_does_not(tmp_path, pi):
    # With A on the pin, the engine script reads pending 0 and the green half cannot hold.
    a, b = _engine_builds(tmp_path)
    pin = _engine_pin(pi)
    assert fnmatchcase(fx.deb_version(b), pin), (fx.deb_version(b), pin)
    assert not fnmatchcase(fx.deb_version(a), pin), (fx.deb_version(a), pin)


# The red fixture.


def _nfpm(directory: Path) -> dict:
    return yaml_fast.safe_load((directory / "nfpm.yaml").read_text())


def test_the_red_fixture_is_the_real_pi_package_with_one_exact_depends_on_b(
    tmp_path, pi
):
    variant = _function(DEPENDS_VARIANT)
    _, b = _engine_builds(tmp_path)
    version = fx.deb_version(b)

    red = _nfpm(variant(tmp_path / "red", version))

    host_mode.write_packages([pi], tmp_path / "real")
    real = _nfpm(tmp_path / "real" / pi.directory)
    assert red["name"] == fx.HOST_PKG
    assert f"{ENGINE} (= {version})" in red["depends"]
    assert not any(d.split()[0] == ENGINE for d in real["depends"])
    extra = [d for d in red["depends"] if d not in real["depends"]]
    assert extra == [f"{ENGINE} (= {version})"]
    assert {k: v for k, v in red.items() if k != "depends"} == {
        k: v for k, v in real.items() if k != "depends"
    }


# What the red fixture records. `red_outcome(held, after, apt_ok)` takes the held version A,
# docker-ce's installed version after the red package's install ("" when not installed), and
# whether apt exited 0.


def test_an_install_apt_refused_with_the_engine_left_at_a_is_recorded_as_refused():
    red_outcome = _function(RED_OUTCOME)
    assert red_outcome("1.0", "1.0", False) == REFUSED


@pytest.mark.parametrize("apt_ok", [True, False])
def test_an_install_that_left_the_engine_at_another_version_is_recorded_as_moved(
    apt_ok,
):
    red_outcome = _function(RED_OUTCOME)
    assert red_outcome("1.0", "2.0", apt_ok) == MOVED


def test_an_install_apt_accepted_with_the_engine_still_at_a_fails_the_case():
    # Depends (= B) cannot be met at A, so a green install means the red fixture never reached
    # apt's resolution of the hold.
    red_outcome = _function(RED_OUTCOME)
    with pytest.raises(CaseFailed):
        red_outcome("1.0", "1.0", True)


# The green assertion. `expect_held(held, installed, prom)` takes the held version A, what
# `dpkg-query -W docker-ce` read after the updater run, and homelab_host.prom's text.


def test_the_engine_at_a_with_pending_one_passes():
    expect_held = _function(EXPECT_HELD)
    expect_held("1.0", "1.0", OK_LINE + PENDING_1)


@pytest.mark.parametrize(
    ("installed", "prom"),
    [
        ("2.0", OK_LINE + PENDING_1),
        ("", OK_LINE + PENDING_1),
        ("1.0", OK_LINE + PENDING_0),
        ("1.0", OK_LINE),
        ("1.0", OK_LINE + PENDING_1.replace("daniel-pi", "daniel-box")),
    ],
    ids=["moved", "removed", "pending-0", "no-series", "other-host"],
)
def test_an_engine_off_a_or_a_pending_series_not_reading_one_fails(installed, prom):
    expect_held = _function(EXPECT_HELD)
    with pytest.raises(CaseFailed):
        expect_held("1.0", installed, prom)


# Where the job runs the case, and what triggers it.


def _main_runs_the_engine_case(source: str) -> bool:
    """Whether `main` in the cases' source calls `engine_case.run(...)`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "engine_case"
                for call in ast.walk(node)
            )
    return False


def test_the_host_packages_job_runs_the_engine_case():
    source = (fx.REPO / CASES).read_text()
    assert _main_runs_the_engine_case(source)
    # Red proof: the conffile case's run, which main calls already, does not count.
    assert not _main_runs_the_engine_case(
        source.replace("engine_case.run", "conffile_case.run")
    )


def test_case_inputs_names_the_engine_case():
    # The census in ansible/tests/repo/ reaches a case module only once main imports it, so a
    # PR that changes only this case would skip the cases until then.
    jobs = yaml_fast.safe_load((fx.REPO / CI_WORKFLOW).read_text())["jobs"]
    (render,) = [
        step
        for step in jobs["host_packages_build"]["steps"]
        if step.get("id") == "render"
    ]
    assert SOURCE in render["env"]["CASE_INPUTS"].split()
