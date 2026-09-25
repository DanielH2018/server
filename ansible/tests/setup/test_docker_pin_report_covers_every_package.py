"""The behind-pin report covers every pinned Docker package, not docker-ce alone.

`install.yml` installs Docker only on a host that has none, so on daniel-pi a merged Renovate
bump moves the pin and nothing else. The report is the only thing on an ordinary run that says
the host is behind it, and until 2026-09-25 it compared
`docker_install_package_versions['docker-ce']` alone -- while `defaults/main.yml` pins
containerd.io, the compose plugin and the buildx plugin independently and four Renovate
managers track them (#2407). A bump to one of those four printed nothing; only
`engine-upgrade.yml`'s apt probe saw it, and only when someone ran that play.

Split out of `test_docker_engine_is_held_before_apt_upgrade.py` when that file crossed the
module-length cap. The hold census, the apt-spec glob and the deliberate upgrade stay there;
this file owns the report and the per-package read under it.

Run: uv run pytest ansible/tests/setup/test_docker_pin_report_covers_every_package.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import SETUP_ROLES

INSTALL = SETUP_ROLES / "docker_install" / "tasks" / "install.yml"
DEFAULTS = SETUP_ROLES / "docker_install" / "defaults" / "main.yml"

VERSIONS_VAR = "docker_install_package_versions"
SUFFIX_VAR = "docker_install_apt_suffix"
# The register the per-package read fills, one result per key of VERSIONS_VAR.
PINS_REGISTER = "docker_install_installed_pins"


def load_tasks(path: Path) -> list[dict]:
    return [
        t for t in yaml_fast.safe_load(path.read_text()) or [] if isinstance(t, dict)
    ]


def behind_pin_reports(tasks: list[dict]) -> list[dict]:
    """Every debug task that reports an installed package behind its pin.

    Matched on `when` OR `loop`, because the two shapes name the read in different keys: the
    pre-#2407 report read the scalar `docker_install_installed_engine` in its `when`, the
    looped one reads `docker_install_installed_pins` in its `loop` and `item.stdout` in its
    `when`.
    """
    out = []
    for task in tasks:
        if not isinstance(task.get("ansible.builtin.debug"), dict):
            continue
        if "docker_install_installed" in report_blob(task):
            out.append(task)
    return out


def report_blob(task: dict) -> str:
    """The report's comparison surface -- its `when` conditions plus its `loop` source."""
    when = task.get("when", "")
    conditions = when if isinstance(when, list) else [when]
    return " ".join(str(c) for c in conditions) + " " + str(task.get("loop", ""))


def report_compared_packages(task: dict, versions: dict) -> set[str]:
    """Which pinned packages this report's comparison actually reads.

    A report looping over the per-package read register compares whatever that register holds,
    which is one result per key of the versions dict. A report naming keys literally --
    `docker_install_package_versions['docker-ce']` -- compares only the keys it names, and a
    merged bump to any other pin prints nothing.
    """
    blob = report_blob(task)
    if PINS_REGISTER in blob:
        return set(versions)
    return {name for name in versions if f"'{name}'" in blob or f'"{name}"' in blob}


def test_the_behind_pin_report_compares_every_pinned_package():
    """A report covering docker-ce alone is silent on the four other pins (#2407).

    `defaults/main.yml` pins containerd.io, the compose plugin and the buildx plugin
    independently, and four Renovate managers track them. A merged bump to one of those alone
    moves the pin and leaves the installed host behind it, with nothing on an ordinary run
    saying so -- only `engine-upgrade.yml`'s apt probe sees it, and only when someone runs
    that play.
    """
    versions = yaml_fast.safe_load(DEFAULTS.read_text())[VERSIONS_VAR]
    reports = behind_pin_reports(load_tasks(INSTALL))
    assert reports, (
        "install.yml no longer reports an installed package behind its pin, so the gap is "
        "invisible behind a green run again"
    )
    covered = report_compared_packages(reports[0], versions)
    missing = set(versions) - covered
    assert not missing, (
        f"the behind-pin report compares {sorted(covered)} and not {sorted(missing)}, so a "
        "merged Renovate bump to those prints nothing on an installed host"
    )


def test_the_behind_pin_report_reads_the_upstream_version_not_the_glob():
    """A `!=` against the globbed suffix is true on every host, pinned correctly or not.

    The report would fire on every run, and an operator reading "Nothing moved" would take a
    correctly-pinned host for one carrying a standing gap (#2357).
    """
    reports = behind_pin_reports(load_tasks(INSTALL))
    assert reports, "install.yml no longer reports an installed package behind its pin"
    blob = report_blob(reports[0])
    assert SUFFIX_VAR not in blob, (
        f"the behind-pin report compares the installed version against {SUFFIX_VAR}, which "
        "globs the revision -- no dpkg version equals it, so the report always fires"
    )
    assert "split('-') | first" in blob, (
        "the behind-pin report no longer takes the upstream version (everything left of the "
        "first `-`) off the installed string, so it compares a Debian revision against a pin "
        "that names none"
    )


def test_the_per_package_read_covers_every_pin_and_leaves_the_install_gate_scalar():
    """The looped read is a SECOND task; `Install Docker` keeps its scalar register.

    Looping the docker-ce read instead of adding one would move its value to `.results`, and
    `Install Docker`'s `when: ... .stdout | length == 0` would then be false on a fresh host --
    a gate that silently stops gating, which is worse than the gap #2407 closed.
    """
    tasks = load_tasks(INSTALL)
    reads = [
        t
        for t in tasks
        if t.get("register") == PINS_REGISTER
        and isinstance(t.get("ansible.builtin.command"), dict)
    ]
    assert len(reads) == 1, (
        f"expected one task registering {PINS_REGISTER}, found {reads}"
    )
    read = reads[0]
    assert read["loop"] == "{{ " + VERSIONS_VAR + " | dict2items }}", (
        f"the per-package read no longer loops over every key of {VERSIONS_VAR}, so the "
        "report below it covers a subset of the pins"
    )
    for flag, value in (
        ("changed_when", False),
        ("failed_when", False),
        ("check_mode", False),
    ):
        assert read.get(flag) is value, (
            f"the per-package read dropped `{flag}: false`; dpkg-query exits 1 on a package "
            "that is not installed, and a read skipped under --check leaves the report "
            "reading an undefined .results"
        )
    installs = [
        t
        for t in tasks
        if isinstance(t.get("ansible.builtin.apt"), dict)
        and "docker_install_installed_engine" in str(t.get("when", ""))
    ]
    assert installs, (
        "no apt task gates on docker_install_installed_engine any more -- the fresh-host "
        "install gate reads something else, or the read was looped in place"
    )
    assert ".stdout" in str(installs[0]["when"]), (
        "the fresh-host install gate no longer reads a scalar .stdout, so the docker-ce read "
        "was turned into a loop and the gate is always false"
    )


def test_a_docker_ce_only_report_is_detected():
    """The pre-#2407 shape: a `when` naming one key, against a dict of five."""
    versions = {
        "docker-ce": "5:1",
        "docker-ce-cli": "5:1",
        "containerd.io": "1",
        "docker-compose-plugin": "1",
        "docker-buildx-plugin": "1",
    }
    old = {
        "ansible.builtin.debug": {"msg": "..."},
        "when": [
            "docker_install_installed_engine.stdout | length > 0",
            "(docker_install_installed_engine.stdout | split('-') | first) "
            f"!= {VERSIONS_VAR}['docker-ce']",
        ],
    }
    assert behind_pin_reports([old]) == [old]
    assert report_compared_packages(old, versions) == {"docker-ce"}
    looped = {
        "ansible.builtin.debug": {"msg": "..."},
        "loop": "{{ " + PINS_REGISTER + ".results }}",
        "when": ["item.stdout | length > 0"],
    }
    assert report_compared_packages(looped, versions) == set(versions)
