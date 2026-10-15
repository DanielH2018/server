"""The signer's run: its refusal rule, `Valid-Until`, the daily re-sign, the hold and the metric.

Each test drives the real tools: `dpkg-deb` builds the fixture packages, `apt-ftparchive`
writes the indexes and the `Release`, and `gpg` signs with a throwaway ed25519 key generated
once per module. Only the paths and the clock are the test's. The cases are the *Signer
refusal* and *Valid-Until* items of docs/target-architecture/host/h3-pi-pilot.md,
*Verification*.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_repo_signer.py
"""

import ast
import email.utils
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import homelab_repo_sign as rs
from host_package_builder import version

SCRIPT = Path(rs.__file__)
CHOSEN = "a594ad662e68"
OTHER = "0123456789ab"


def run(*argv: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, capture_output=True, timeout=60, **kwargs)


@pytest.fixture(scope="module")
def keyhome(tmp_path_factory) -> tuple[Path, str]:
    """A GnuPG home holding one signing key, and that key's fingerprint."""
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback"]
    run(
        *gpg,
        *("--passphrase", "", "--quick-generate-key", "t <t@example.invalid>"),
        *("ed25519", "sign", "never"),
    )
    listing = run(*gpg, "--with-colons", "--list-keys").stdout.decode()
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    run("gpgconf", "--homedir", str(home), "--kill", "gpg-agent")
    return home, fingerprint


class Lab:
    """The two state directories, the operator's hold and archive-key.fpr, under tmp_path."""

    def __init__(self, root: Path, keyhome: tuple[Path, str]):
        home, self.fingerprint = keyhome
        self.root = root
        self.staged = root / "builder" / "staged"
        self.staged.mkdir(parents=True)
        self.signer_dir = root / "signer"
        (self.signer_dir / "repo").mkdir(parents=True)
        # Without /run/user/<uid>, gpg-agent puts its sockets in the home, and a socket
        # cannot be copied.
        shutil.copytree(
            home, self.signer_dir / "gnupg", ignore=shutil.ignore_patterns("S.*")
        )
        self.etc = root / "etc"
        self.etc.mkdir()
        self.hold = self.etc / "hold"
        (self.etc / "archive-key.fpr").write_text(self.fingerprint + "\n")
        self.clock = 1_800_000_000.0
        self.builds = 0

    def signer(self) -> rs.Signer:
        return rs.Signer(
            hold_path=self.hold,
            fingerprint_path=self.etc / "archive-key.fpr",
            staged=self.staged,
            signer_dir=self.signer_dir,
            now=lambda: self.clock,
        )

    def deb(self, package: str, version: str, name: str | None = None) -> Path:
        """Build a minimal `.deb` with dpkg-deb and stage it under `name`."""
        self.builds += 1
        tree = self.root / "build" / str(self.builds)
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN" / "control").write_text(
            f"Package: {package}\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: t <t@example.invalid>\nDescription: fixture\n"
        )
        target = self.staged / (name or f"{package}_{version}_all.deb")
        # --nocheck: dpkg-deb otherwise refuses a control directory outside 0755..0775,
        # which the session umask decides.
        run(
            "dpkg-deb",
            "--nocheck",
            "--root-owner-group",
            "--build",
            str(tree),
            str(target),
        )
        return target

    def stage(self, *packages: str, sha: str = CHOSEN, count: int = 5) -> None:
        # The builder's own version(), so a change to its format fails here first.
        (self.staged / "chosen").write_text(sha + "\n")
        for package in packages:
            self.deb(package, version(count, sha))

    @property
    def suite(self) -> Path:
        return self.signer_dir / "repo" / "dists" / "homelab"

    def release(self) -> dict[str, str]:
        return rs.release_fields((self.suite / "Release").read_text())

    def prom(self) -> list[str]:
        text = (self.signer_dir / "homelab_repo.prom").read_text()
        return [line for line in text.splitlines() if not line.startswith("#")]


@pytest.fixture
def lab(tmp_path, keyhome) -> Lab:
    return Lab(tmp_path, keyhome)


def test_the_signer_parses_with_the_3_12_grammar():
    # fact: ansible/roles/setup/homelab_builder/CLAUDE.md#The signer and the publish (H3.5b)
    # daniel-box runs it under Ubuntu 24.04's python3. A 3.14-only construct, such as an
    # unparenthesised `except A, B:`, would fail every run there and none here.
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


def test_a_3_14_only_construct_fails_the_3_12_grammar():
    with pytest.raises(SyntaxError):
        ast.parse(
            "try:\n    pass\nexcept OSError, ValueError:\n    pass\n",
            feature_version=(3, 12),
        )


def test_a_staged_package_at_the_chosen_commit_is_published_and_signed(lab):
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    pool = lab.signer_dir / "repo" / "pool" / "main"
    assert [p.name for p in pool.iterdir()] == [
        f"homelab-daniel-pi_0.5+g{CHOSEN}_all.deb"
    ]
    for arch in ("amd64", "arm64"):
        index = (lab.suite / "main" / f"binary-{arch}" / "Packages").read_text()
        assert f"Filename: pool/main/homelab-daniel-pi_0.5+g{CHOSEN}_all.deb" in index
    assert lab.release()["Suite"] == "homelab"
    assert lab.release()["Architectures"] == "amd64 arm64"
    for signed in ("InRelease", "Release.gpg"):
        argv = ["gpg", "--homedir", str(lab.signer_dir / "gnupg"), "--verify"]
        if signed == "Release.gpg":
            argv += [str(lab.suite / signed), str(lab.suite / "Release")]
        else:
            argv += [str(lab.suite / signed)]
        assert lab.fingerprint in run(*argv).stderr.decode()
    assert os.listdir(lab.staged) == []
    assert lab.prom() == [
        f'homelab_repo_published_info{{package="homelab-daniel-pi",sha="{CHOSEN}",'
        f'version="0.5+g{CHOSEN}"}} 1'
    ]


def test_the_release_is_valid_for_seven_days_from_its_date(lab):
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    fields = lab.release()
    date = email.utils.parsedate_to_datetime(fields["Date"])
    valid_until = email.utils.parsedate_to_datetime(fields["Valid-Until"])
    assert (valid_until - date).total_seconds() == 7 * 24 * 3600


def test_every_published_file_is_world_readable(lab):
    # homelab-apt-repo.service is a DynamicUser: it reads the tree through the other bits.
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    repo = lab.signer_dir / "repo"
    seen = 0
    for directory, _subdirs, files in os.walk(repo, followlinks=True):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o755, directory
        for name in files:
            seen += 1
            mode = stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode)
            assert mode == 0o644, os.path.join(directory, name)
    assert seen >= 7, "the walk found too few files to mean anything"


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(
            lambda lab: lab.deb("other-tool", f"1.0+g{CHOSEN}"), id="other-package"
        ),
        pytest.param(
            lambda lab: lab.deb("homelab-daniel-pi", f"0.4+g{OTHER}"), id="another-sha"
        ),
        pytest.param(
            lambda lab: (lab.staged / "notes.txt").write_text("x"), id="stray-file"
        ),
        pytest.param(
            lambda lab: (lab.staged / "homelab-x_1_all.deb").symlink_to(
                lab.signer_dir / "gnupg" / "pubring.kbx"
            ),
            id="symlink",
        ),
        pytest.param(
            lambda lab: (lab.staged / "chosen").write_text("main\n"), id="bad-chosen"
        ),
        # A version reaches a pool file name and a metric label.
        pytest.param(
            lambda lab: lab.deb(
                "homelab-updater", f"0.5/../../x+g{CHOSEN}", name="homelab-updater.deb"
            ),
            id="slash-in-version",
        ),
        pytest.param(
            lambda lab: lab.deb(
                "homelab-updater", f'0.5"}}x+g{CHOSEN}', name="homelab-updater.deb"
            ),
            id="quote-in-version",
        ),
    ],
)
def test_a_staged_set_the_rule_refuses_publishes_nothing(lab, stage):
    lab.stage("homelab-daniel-pi")
    stage(lab)
    assert lab.signer().main() == 1
    assert not (lab.signer_dir / "repo" / "dists").exists(), "it published"
    assert not (lab.signer_dir / "repo" / "pool" / "main").exists()
    assert os.listdir(lab.staged) == []
    [refused] = (lab.signer_dir / "refused").iterdir()
    assert "chosen" in os.listdir(refused)


def test_a_symlinked_entry_is_recorded_without_its_target(lab):
    lab.stage("homelab-daniel-pi")
    (lab.staged / "homelab-x_1_all.deb").symlink_to(
        lab.signer_dir / "gnupg" / "pubring.kbx"
    )
    assert lab.signer().main() == 1
    [refused] = (lab.signer_dir / "refused").iterdir()
    assert (refused / "homelab-x_1_all.deb.not-a-file").read_bytes() == b""
    assert not (refused / "homelab-x_1_all.deb").exists()


def test_a_hold_leaves_staged_alone_and_publishes_nothing(lab):
    lab.stage("homelab-daniel-pi")
    lab.hold.touch()
    assert lab.signer().main() == 0
    assert sorted(os.listdir(lab.staged)) == [
        "chosen",
        f"homelab-daniel-pi_0.5+g{CHOSEN}_all.deb",
    ]
    assert not (lab.signer_dir / "repo" / "dists").exists()
    # Releasing the hold publishes the set at the next run.
    lab.hold.unlink()
    assert lab.signer().main() == 0
    assert (lab.suite / "InRelease").is_file()


def test_a_release_25_hours_old_is_re_signed_and_one_an_hour_old_is_not(lab):
    # apt-ftparchive dates the Release from the real clock, and the signer's staleness check
    # reads the test's, so each run's clock is set relative to that Date.
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    first = os.readlink(lab.signer_dir / "repo" / "dists")
    published = email.utils.parsedate_to_datetime(lab.release()["Date"]).timestamp()

    lab.clock = published + 3600
    assert lab.signer().main() == 0
    assert os.readlink(lab.signer_dir / "repo" / "dists") == first

    lab.clock = published + 25 * 3600
    assert lab.signer().main() == 0
    assert os.readlink(lab.signer_dir / "repo" / "dists") != first
    assert len(list((lab.signer_dir / "repo" / ".snapshots").iterdir())) == 1


def test_a_hold_still_re_signs_a_stale_release(lab):
    # The hold stops publishing, never the re-sign: Valid-Until would otherwise lapse on
    # every host a week into a hold.
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    first = os.readlink(lab.signer_dir / "repo" / "dists")
    lab.stage("homelab-daniel-pi", count=6)
    lab.hold.touch()
    published = email.utils.parsedate_to_datetime(lab.release()["Date"]).timestamp()
    lab.clock = published + 25 * 3600
    assert lab.signer().main() == 0
    assert os.readlink(lab.signer_dir / "repo" / "dists") != first
    assert (
        "Version: 0.6"
        not in (lab.suite / "main" / "binary-amd64" / "Packages").read_text()
    )
    assert "chosen" in os.listdir(lab.staged)


def test_a_new_signing_key_re_signs_a_fresh_release(lab):
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    state = lab.signer_dir / "state.json"
    recorded = json.loads(state.read_text())
    assert recorded["signed_by"] == lab.fingerprint
    state.write_text(json.dumps({**recorded, "signed_by": "0" * 40}))
    first = os.readlink(lab.signer_dir / "repo" / "dists")
    lab.clock = email.utils.parsedate_to_datetime(lab.release()["Date"]).timestamp()
    assert lab.signer().main() == 0
    assert os.readlink(lab.signer_dir / "repo" / "dists") != first


def test_the_metric_keeps_every_package_a_later_run_did_not_restage(lab):
    lab.stage("homelab-daniel-pi", "homelab-updater", count=5)
    assert lab.signer().main() == 0
    lab.stage("homelab-daniel-pi", sha=OTHER, count=6)
    assert lab.signer().main() == 0
    assert lab.prom() == [
        f'homelab_repo_published_info{{package="homelab-daniel-pi",sha="{OTHER}",'
        f'version="0.6+g{OTHER}"}} 1',
        f'homelab_repo_published_info{{package="homelab-updater",sha="{CHOSEN}",'
        f'version="0.5+g{CHOSEN}"}} 1',
    ]


def test_a_missing_fingerprint_pages_once_then_stays_quiet(lab):
    (lab.etc / "archive-key.fpr").unlink()
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 1
    assert lab.signer().main() == 0
    assert "chosen" in os.listdir(lab.staged), (
        "a failed run must keep the set for the next"
    )


def test_a_tool_that_will_not_run_pages_once_and_leaves_the_pool_as_it_was(lab):
    # Every successful builder run starts the signer, so an error that escaped as a
    # traceback would page every 2 minutes.
    lab.stage("homelab-daniel-pi")
    signer = lab.signer()
    signer.apt_ftparchive = str(lab.root / "absent-apt-ftparchive")
    assert signer.main() == 1
    assert json.loads((lab.signer_dir / "state.json").read_text())["failure"] == {
        "reason": "io"
    }
    signer = lab.signer()
    signer.apt_ftparchive = str(lab.root / "absent-apt-ftparchive")
    assert signer.main() == 0
    assert list((lab.signer_dir / "repo" / "pool" / "main").iterdir()) == []


def test_a_failed_publish_never_rides_along_on_a_later_held_re_sign(lab):
    lab.stage("homelab-daniel-pi")
    assert lab.signer().main() == 0
    lab.stage("homelab-daniel-pi", count=6)
    # gpg cannot sign with a key the home does not hold, so this publish fails at `sign`.
    (lab.etc / "archive-key.fpr").write_text("F" * 40 + "\n")
    assert lab.signer().main() == 1
    pool = lab.signer_dir / "repo" / "pool" / "main"
    assert [p.name for p in pool.iterdir()] == [
        f"homelab-daniel-pi_0.5+g{CHOSEN}_all.deb"
    ]
    # A held re-sign once the key is back indexes the pool as it then is.
    (lab.etc / "archive-key.fpr").write_text(lab.fingerprint + "\n")
    lab.hold.touch()
    lab.clock = (
        email.utils.parsedate_to_datetime(lab.release()["Date"]).timestamp() + 25 * 3600
    )
    assert lab.signer().main() == 0
    index = (lab.suite / "main" / "binary-amd64" / "Packages").read_text()
    assert f"Version: 0.5+g{CHOSEN}" in index
    assert "Version: 0.6" not in index
