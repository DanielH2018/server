"""The age bootstrap case's verdicts, without a container (#8041).

`validate_lib/bootstrap_age_case.py` runs only in CI's `host_packages` job, which has Docker.
This holds what it decides from a container's output: a green run passes, a red run fails, and a
red run while snapshot.ubuntu.com answers 5xx gives no verdict.

Run: uv run pytest scripts/validate/tests/test_bootstrap_age_case.py
"""

import subprocess
from types import SimpleNamespace

import pytest
from validate.validate_lib import bootstrap_age_case as age_case
from validate.validate_lib.host_container import CaseFailed, Container


def _fake_age_container(monkeypatch, stdout, returncode=0, stderr="") -> list[str]:
    """Stub `Container`'s docker calls; returns the list each command is appended to."""
    commands = []

    def fake_ok(self, command):
        commands.append(command)
        return ""

    def fake_sh(self, command, timeout=None):
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(Container, "boot", lambda self, python: None)
    monkeypatch.setattr(Container, "ok", fake_ok)
    monkeypatch.setattr(Container, "sh", fake_sh)
    monkeypatch.setattr(Container, "remove", lambda self: None)
    return commands


def _age_run(monkeypatch, tmp_path, stdout: str, returncode=0, stderr="") -> list[str]:
    """Run `expect_age_from_apt` on a fake container; returns what it ran, in order."""
    commands = _fake_age_container(monkeypatch, stdout, returncode, stderr)
    age_case.expect_age_from_apt(Container("img", "c", tmp_path), "3.12")
    return commands


# apt's lines from run 43187866124, where snapshot.ubuntu.com answered 502 and 500.
DEAD_HOMELAB = (
    "W: Failed to fetch http://127.0.0.1:9/dists/homelab/InRelease  Could not connect to"
    " 127.0.0.1:9 (127.0.0.1). - connect (111: Connection refused)\n"
)
SNAPSHOT = (
    "https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/dists/noble-updates/InRelease"
)
NO_AGE = (
    "E: Unable to locate package age\n"
    "bootstrap-host: cannot install age, whose age-keygen creates and reads"
    " /etc/homelab/age.key\n"
)


def test_a_run_that_installed_age_and_printed_the_key_is_clean(monkeypatch, tmp_path):
    stdout = (
        "bootstrap-host: installed age\n"
        "bootstrap-host: age public key for daniel-pi: age1abc\n"
    )
    purge, script = _age_run(monkeypatch, tmp_path, stdout)

    assert "dpkg --purge age" in purge and "/var/lib/apt/lists" in purge
    assert script.endswith("bootstrap-host.sh daniel-pi --until 3")


def test_a_run_that_found_age_already_installed_is_flagged(monkeypatch, tmp_path):
    stdout = "bootstrap-host: age public key for daniel-pi: age1abc\n"
    with pytest.raises(CaseFailed, match="installed age"):
        _age_run(monkeypatch, tmp_path, stdout)


def test_a_snapshot_5xx_on_a_failed_run_gives_no_verdict(monkeypatch, tmp_path, capsys):
    stderr = (
        DEAD_HOMELAB
        + f"W: Failed to fetch {SNAPSHOT}  502  Bad Gateway [IP: 185.125.189.69 443]\n"
        + NO_AGE
    )
    _fake_age_container(monkeypatch, "", returncode=1, stderr=stderr)
    cases = SimpleNamespace(
        work=tmp_path, image="img", python="3.12", failed=[], no_verdict=[]
    )

    age_case.run(cases)

    assert (cases.failed, cases.no_verdict) == ([], [age_case.NAME])
    assert f"[NO VERDICT] {age_case.NAME}: Failed to fetch {SNAPSHOT}  502" in (
        capsys.readouterr().err
    )


def test_a_snapshot_5xx_on_a_run_that_died_past_the_install_is_flagged(
    monkeypatch, tmp_path
):
    stderr = (
        f"W: Failed to fetch {SNAPSHOT}  502  Bad Gateway [IP: 185.125.189.69 443]\n"
        "bootstrap-host: the age package installed no age-keygen\n"
    )
    with pytest.raises(CaseFailed, match="exited 1"):
        _age_run(monkeypatch, tmp_path, "", returncode=1, stderr=stderr)


def test_a_failed_run_with_only_the_dead_homelab_source_is_flagged(
    monkeypatch, tmp_path
):
    with pytest.raises(CaseFailed, match="exited 1"):
        _age_run(monkeypatch, tmp_path, "", returncode=1, stderr=DEAD_HOMELAB + NO_AGE)


def test_a_snapshot_404_on_a_failed_run_is_flagged(monkeypatch, tmp_path):
    stderr = f"W: Failed to fetch {SNAPSHOT}  404  Not Found [IP: 185.125.189.69 443]\n"
    with pytest.raises(CaseFailed, match="exited 1"):
        _age_run(monkeypatch, tmp_path, "", returncode=1, stderr=stderr + NO_AGE)
