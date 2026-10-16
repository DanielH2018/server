"""The container cases' fixtures, repository writer and log checks, without a container.

The cases themselves run only in CI's `host_packages` job, which has Docker and the pinned
nfpm. These tests cover what the cases stand on: the fixture host packages the real renderer
builds, the unsigned repository apt reads, and the two checks every case applies to the server
log and the apt lists. The `.deb` here is built with `dpkg-deb --build`, as in
`test_validate_host_packages.py`.

Run: uv run pytest scripts/validate/tests/test_validate_host_package_cases.py
"""

import ast
import getpass
import hashlib
import importlib
import inspect
import subprocess
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from lib import proc_testing
from renderer.renderer_lib import host as host_mode
from validate import host_package_cases as cases
from validate import host_packages
from validate.validate_lib import host_fixtures as fx

BOX = "10.0.0.215"
# The names the signed fixture repository adds (#5147). Each is reached through getattr, as the
# fix adds it: the keyword `publish` takes for the moment the Release stops being valid, and the
# file name, inside the published repository, of the public keyring that verifies its InRelease.
VALID_UNTIL = "valid_until"
TEST_KEYRING = "TEST_KEYRING"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("fixtures")
    return {
        variant: fx.render_fixture(variant, out / variant, payload_bytes=16)
        for variant in fx.VARIANTS
    }


def _installed(directory: Path) -> set[str]:
    root = directory / "root"
    return {f"/{path.relative_to(root)}" for path in root.rglob("*") if path.is_file()}


def test_the_head_fixture_drops_a_unit_and_a_file_and_depends_on_tree(fixtures):
    dropped = _installed(fixtures["base"]) - _installed(fixtures["head"])
    assert dropped == {
        "/usr/lib/homelab/fixture-dropped",
        "/usr/lib/systemd/system/homelab-fixture-dropped.service",
        "/usr/lib/systemd/system/multi-user.target.wants/homelab-fixture-dropped.service",
    }
    assert "- tree" in (fixtures["head"] / "nfpm.yaml").read_text()
    assert "- tree" not in (fixtures["base"] / "nfpm.yaml").read_text()


def test_the_payload_fixture_differs_from_the_head_fixture_as_named(fixtures):
    assert _installed(fixtures["payload"]) - _installed(fixtures["head"]) == {
        fx.PAYLOAD
    }
    payload = fixtures["payload"] / f"root{fx.PAYLOAD}"
    assert payload.stat().st_size == 16


@pytest.mark.parametrize("variant", fx.VARIANTS)
def test_no_fixture_ships_the_updater_timers_enable_link(fixtures, variant):
    # A link would let reconcile start the timer, whose runs would race the cases.
    links = [path for path in _installed(fixtures[variant]) if ".wants/" in path]
    assert links
    assert not [link for link in links if link.endswith("/homelab-updater.timer")]


@pytest.mark.parametrize("variant", fx.VARIANTS)
def test_every_fixtures_maintainer_scripts_pass_the_lint(fixtures, variant):
    scripts = sorted((fixtures[variant] / "scripts").iterdir())
    assert [script.name for script in scripts] == ["postinst", "preinst", "prerm"]
    for script in scripts:
        assert host_packages.lint_script(script.name, script.read_text()) == []


def test_the_updater_variant_changes_its_unit_and_keeps_the_keyring(tmp_path):
    head = tmp_path / "head"
    host_mode.write_packages([host_mode.render_package(fx.UPDATER)], head)

    variant = fx.updater_variant(head, tmp_path / "variant")

    unit = f"root{fx.UNIT_DIR}/{fx.UPDATER}.service"
    assert (variant / unit).read_text() == (
        (head / "packages" / fx.UPDATER / unit).read_text() + fx.UNIT_CHANGE + "\n"
    )
    keyring = f"root{fx.KEYRING}"
    assert (variant / keyring).read_bytes() == (
        head / "packages" / fx.UPDATER / keyring
    ).read_bytes()
    assert (variant / "nfpm.yaml").read_text().count(f"dst: {fx.KEYRING}") == 1


def test_the_pi_dropin_variant_changes_only_the_real_dropin(tmp_path):
    head = tmp_path / "head"
    host_mode.write_packages([host_mode.render_host(fx.HOST)], head)
    real = head / "hosts" / fx.HOST

    variant = fx.pi_dropin_variant(head, tmp_path / "variant")

    dropin = f"root{fx.DROPIN}"
    assert (variant / dropin).read_text() == (real / dropin).read_text().replace(
        fx.DROPIN_MINUTE, fx.DROPIN_CHANGED
    )
    assert fx.DROPIN_CHANGED in (variant / dropin).read_text()
    assert _installed(variant) == _installed(real)
    assert (variant / "nfpm.yaml").read_text() == (real / "nfpm.yaml").read_text()


def test_a_pi_render_whose_dropin_moved_is_refused(tmp_path):
    head = tmp_path / "head"
    host_mode.write_packages([host_mode.render_host(fx.HOST)], head)
    dropin = head / "hosts" / fx.HOST / f"root{fx.DROPIN}"
    dropin.write_text(dropin.read_text().replace(fx.DROPIN_MINUTE, ":07/15:00"))

    with pytest.raises(ValueError, match="does not fire at"):
        fx.pi_dropin_variant(head, tmp_path / "variant")


def test_the_box_address_comes_from_the_shipped_source(tmp_path):
    host_mode.write_packages([host_mode.render_package(fx.UPDATER)], tmp_path)
    assert fx.box_address(tmp_path) == BOX


def test_a_published_repository_indexes_each_deb(tmp_path):
    root = tmp_path / "pkg"
    control = root / "DEBIAN"
    control.mkdir(parents=True)
    control.chmod(0o755)
    (control / "control").write_text(
        "Package: homelab-fixture\nVersion: 0.1+gaaaaaaaaaaaa\nArchitecture: all\n"
        "Maintainer: homelab\nDescription: fixture\n"
    )
    deb = tmp_path / "homelab-fixture_0.1_all.deb"
    proc_testing.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)])
    repo = tmp_path / "repo"
    repo.mkdir()

    fx.publish(repo, [deb])

    index = (repo / "dists/homelab/main/binary-amd64/Packages").read_bytes()
    assert b"Package: homelab-fixture\n" in index
    assert b"Filename: pool/homelab-fixture_0.1_all.deb\n" in index
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    assert f"SHA256: {digest}\n".encode() in index
    release = (repo / "dists/homelab/Release").read_text()
    entry = (
        f" {hashlib.sha256(index).hexdigest()} {len(index)} main/binary-amd64/Packages"
    )
    assert entry in release.splitlines()
    assert "Suite: homelab" in release.splitlines()


def test_a_server_log_with_the_release_get_is_clean():
    log = '127.0.0.1 - - [14/Oct/2026 10:00:00] "GET /dists/homelab/Release HTTP/1.1" 200 -\n'
    assert cases.release_fetched(log)


def test_a_server_log_without_the_release_get_is_flagged():
    log = '127.0.0.1 - - [14/Oct/2026 10:00:00] "GET / HTTP/1.1" 200 -\n'
    assert not cases.release_fetched(log)


def test_lists_from_the_test_repository_alone_are_clean():
    names = ["127.0.0.1:8791_dists_homelab_Release", "lock", "partial"]
    assert cases.list_problems(names, BOX) == []


@pytest.mark.parametrize(
    ("names", "problem"),
    [
        (
            [
                "127.0.0.1:8791_dists_homelab_Release",
                "10.0.0.215:8791_dists_homelab_Release",
            ],
            "named after daniel-box's address",
        ),
        (["lock", "partial"], "no list for 127.0.0.1:8791"),
    ],
)
def test_a_box_list_or_no_test_list_is_flagged(names, problem):
    problems = cases.list_problems(names, BOX)
    assert len(problems) == 1
    assert problem in problems[0]


def _deb(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    control = root / "DEBIAN"
    control.mkdir(parents=True)
    control.chmod(0o755)
    (control / "control").write_text(
        "Package: homelab-fixture\nVersion: 0.1+gaaaaaaaaaaaa\nArchitecture: all\n"
        "Maintainer: homelab\nDescription: fixture\n"
    )
    deb = tmp_path / "homelab-fixture_0.1_all.deb"
    proc_testing.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)])
    return deb


def _publish(tmp_path: Path, **kwargs) -> Path:
    """Publish one deb with `host_fixtures.publish`, reached afresh so new keywords type-check."""
    module = importlib.import_module("validate.validate_lib.host_fixtures")
    taken = inspect.signature(module.publish).parameters
    assert set(kwargs) <= set(taken), (
        f"publish takes no {sorted(set(kwargs) - set(taken))}"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    module.publish(repo, [_deb(tmp_path)], **kwargs)
    return repo


def _keyring(repo: Path) -> Path:
    module = importlib.import_module("validate.validate_lib.host_fixtures")
    assert hasattr(module, TEST_KEYRING), (
        "host_fixtures names no keyring for the repository"
    )
    return repo / getattr(module, TEST_KEYRING)


def _verified_release(repo: Path, tmp_path: Path) -> dict[str, str]:
    """The fields of the InRelease body that gpgv verified against the test keyring."""
    home = tmp_path / "gpgv-home"
    home.mkdir(mode=0o700)
    done = proc_testing.run(
        ["gpgv", "--homedir", str(home), "--keyring", str(_keyring(repo))]
        + ["--output", "-", str(repo / "dists/homelab/InRelease")]
    )
    assert done.returncode == 0, done.stderr
    return {
        key: value.strip()
        for key, _, value in (
            line.partition(":") for line in done.stdout.splitlines() if line[:1] != " "
        )
    }


def _apt_update(repo: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run updater step 3's fetch, unprivileged, against `repo` trusted through its keyring."""
    apt = tmp_path / "apt"
    for directory in ("etc/sources.list.d", "etc/apt.conf.d", "etc/preferences.d"):
        (apt / directory).mkdir(parents=True)
    for directory in ("state/lists/partial", "cache/archives/partial"):
        (apt / directory).mkdir(parents=True)
    (apt / "state/status").write_text("")
    (apt / "etc/sources.list.d/homelab.sources").write_text(
        f"Types: deb\nURIs: file://{repo}\nSuites: homelab\nComponents: main\n"
        f"Signed-By: {_keyring(repo)}\n"
    )
    return proc_testing.run(
        ["apt-get", "update", "--error-on=any"]
        + ["-o", f"Dir::Etc={apt / 'etc'}", "-o", f"Dir::State={apt / 'state'}"]
        + ["-o", f"Dir::State::status={apt / 'state/status'}"]
        + ["-o", f"Dir::Cache={apt / 'cache'}", "-o", "Debug::NoLocking=1"]
        + ["-o", f"APT::Sandbox::User={getpass.getuser()}"]
        + ["-o", "APT::Architecture=amd64"]
    )


def test_a_published_repository_is_signed_by_the_test_keyring(tmp_path):
    repo = _publish(tmp_path)
    assert (repo / "dists/homelab/InRelease").is_file()
    assert _keyring(repo).is_file()
    assert _verified_release(repo, tmp_path)["Suite"] == "homelab"


def test_a_published_release_is_valid_until_after_its_date_and_now(tmp_path):
    fields = _verified_release(_publish(tmp_path), tmp_path)
    assert "Valid-Until" in fields
    valid_until = parsedate_to_datetime(fields["Valid-Until"])
    assert valid_until > parsedate_to_datetime(fields["Date"])
    assert valid_until > datetime.now(UTC)


def test_a_publish_given_a_past_valid_until_signs_that_moment(tmp_path):
    past = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    fields = _verified_release(_publish(tmp_path, **{VALID_UNTIL: past}), tmp_path)
    assert parsedate_to_datetime(fields["Valid-Until"]) == past


def test_apt_accepts_a_fresh_published_repository(tmp_path):
    # The control for the next test: without it, an unsigned or untrusted repository would
    # fail the fetch too, and the expired case would pass for the wrong reason.
    done = _apt_update(_publish(tmp_path), tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_apt_refuses_a_repository_past_its_valid_until(tmp_path):
    past = datetime.now(UTC) - timedelta(hours=1)
    repo = _publish(tmp_path, **{VALID_UNTIL: past})
    done = _apt_update(repo, tmp_path)
    assert done.returncode != 0
    assert "is expired" in done.stderr


def _tracked(*paths: str) -> list[Path]:
    listed = proc_testing.run(
        ["git", "ls-files", "--", *paths], cwd=fx.REPO, check=True
    ).stdout.split()
    return [fx.REPO / name for name in listed]


def test_the_cases_publish_a_repository_past_its_valid_until():
    # The case itself runs only in the host_packages job, which has Docker; this holds that
    # host_package_cases.py serves an expired repository to some case.
    (source,) = _tracked("scripts/validate/host_package_cases.py")
    keywords = {
        keyword.arg
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert VALID_UNTIL in keywords


def test_no_container_source_trusts_the_repository_unsigned():
    # `Trusted: yes` makes apt accept an InRelease signed by any key, or by none, so the case
    # would pass whoever signed the index. The container's source names the test keyring in
    # `Signed-By:` instead, as the hosts' source names the archive key.
    files = _tracked(
        "hosts/lib/tests/container",
        "scripts/validate/validate_lib",
        "scripts/validate/host_package_cases.py",
    )
    assert files
    trusting = [
        str(path.relative_to(fx.REPO))
        for path in files
        if "/tests/test_" not in str(path)
        and any(
            line.strip().strip("'\"").startswith("Trusted: yes")
            for line in path.read_text().splitlines()
        )
    ]
    assert trusting == []


def test_a_server_log_with_the_inrelease_get_is_clean():
    # apt fetches the signed InRelease and never Release once InRelease answers.
    log = '127.0.0.1 - - [14/Oct/2026 10:00:00] "GET /dists/homelab/InRelease HTTP/1.1" 200 -\n'
    assert cases.release_fetched(log)


def test_the_test_images_source_names_the_published_keyring():
    dockerfile = (fx.REPO / "hosts/lib/tests/container/Dockerfile").read_text()
    assert f"'Signed-By: /srv/repo/{fx.TEST_KEYRING}'" in dockerfile
