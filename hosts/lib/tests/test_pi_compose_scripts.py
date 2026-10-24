"""compose-run and compose-adopt against a stub `docker` that logs its argv (H5.2a3).

Each case is one of H5's *Verification* cases for compose-adopt and compose-run
(docs/target-architecture/host/h5-pi-compose.md). A rule that a fixture script can break has a
`_is_flagged` case, so a check that stopped matching fails its own test.

Run: uv run pytest hosts/lib/tests/test_pi_compose_scripts.py
"""

import pytest
from _compose_scripts import (
    ADOPT,
    DIGEST,
    RUN,
    SECRET,
    VERSION,
    Pi,
    config_problems,
    mode_of,
    mutate,
    refusal_problems,
    sha_problems,
)

PROXY_IMAGE = "tecnativa/docker-socket-proxy:latest" + DIGEST


@pytest.fixture
def pi(tmp_path):
    return Pi(tmp_path)


def removals(pi):
    return [line for line in pi.log if line.startswith("rm -f")]


# compose-adopt.


def test_adopt_removes_a_same_name_container_from_another_project(pi):
    pi.containers = {"alloy": "alloy"}

    result = pi.adopt()

    assert result.returncode == 0, result.stderr
    assert removals(pi) == ["rm -f alloy"]
    assert pi.logger == ["-t compose-adopt removing alloy, started by project alloy"]


def test_adopt_leaves_a_container_already_in_homelab_alone(pi):
    pi.containers = {"alloy": "homelab"}

    assert pi.adopt().returncode == 0
    assert removals(pi) == []
    assert pi.logger == []


def test_adopt_never_touches_a_name_the_project_does_not_declare(pi):
    pi.containers = {"portainer": "portainer"}

    assert pi.adopt().returncode == 0
    assert not any("portainer" in line for line in pi.log)


def test_adopt_removes_a_same_name_container_that_carries_no_project_label(pi):
    pi.containers = {"alloy": ""}

    assert pi.adopt().returncode == 0
    assert removals(pi) == ["rm -f alloy"]
    assert pi.logger == ["-t compose-adopt removing alloy, started by project none"]


def test_adopt_runs_config_only_as_services_and_the_check_flags_a_bare_config(
    pi, tmp_path
):
    pi.adopt()
    assert config_problems(pi.log) == []

    fixture = mutate(
        ADOPT,
        tmp_path,
        "config --services)\n",
        'config --services)\ndocker compose -f "$project_file" config\n',
    )
    pi.adopt(fixture)
    assert any("bare config" in p for p in config_problems(pi.log))


# compose-run, the checks that refuse.


@pytest.mark.parametrize("case", ["missing key", "empty value", "no env file"])
def test_a_key_check_refusal_writes_zero_and_removes_and_starts_nothing(pi, case):
    pi.containers = {"alloy": "alloy"}
    if case == "missing key":
        pi.env({"other": "x"})
    elif case == "empty value":
        pi.env({"domain": ""})
    else:
        (pi.compose / "compose.env").unlink()

    result = pi.run()

    assert result.returncode != 0
    assert "domain" in result.stderr
    assert (
        pi.prom()['homelab_compose_up_result{host="daniel-pi",sha="0123456789ab"}']
        == "0"
    )
    assert refusal_problems(pi.log) == []


def test_a_compose_run_that_adopts_before_its_key_check_is_flagged(pi, tmp_path):
    pi.containers = {"alloy": "alloy"}
    pi.env({"other": "x"})
    fixture = mutate(
        RUN,
        tmp_path,
        "  check_keys || return 1\n",
        '  "$adopt" "$project_file" || return 1\n  check_keys || return 1\n',
    )

    pi.run(fixture)

    assert refusal_problems(pi.log) != []


@pytest.mark.parametrize("step", ["config", "pull"])
def test_a_failed_model_check_or_pull_writes_zero_and_removes_and_starts_nothing(
    pi, step
):
    pi.containers = {"alloy": "alloy"}
    pi.present()
    pi.fail(step)

    result = pi.run()

    assert result.returncode != 0
    assert list(pi.prom().values())[0] == "0"
    assert refusal_problems(pi.log) == []


def test_a_run_with_every_image_present_pulls_nothing(pi):
    assert pi.run().returncode == 0
    assert not any(line.startswith("pull") for line in pi.log)


def test_a_run_with_one_image_absent_pulls_that_image_only(pi):
    pi.service("docker-proxy", image="tecnativa/docker-socket-proxy:latest")
    pi.images = {*pi.images, PROXY_IMAGE}

    assert pi.run().returncode == 0
    assert [line for line in pi.log if line.startswith("pull")] == [
        f"pull --quiet {PROXY_IMAGE}"
    ]


def test_an_escaped_dollar_adds_no_key_to_the_checked_set(pi):
    (pi.compose / "alloy" / "compose.yaml").write_text(
        "services:\n  alloy:\n    command: echo $${literal} ${domain}\n"
    )

    result = pi.run()

    assert result.returncode == 0, result.stderr


# compose-run, the start and the metric.


def test_a_failed_up_writes_zero_and_exits_non_zero(pi):
    pi.fail("up")

    assert pi.run().returncode != 0
    assert list(pi.prom().values())[0] == "0"


def test_a_good_up_writes_one_with_a_duration_at_mode_0644(pi):
    result = pi.run()

    assert result.returncode == 0, result.stderr
    prom = pi.prom()
    assert prom['homelab_compose_up_result{host="daniel-pi",sha="0123456789ab"}'] == "1"
    assert int(prom['homelab_compose_up_duration_seconds{host="daniel-pi"}']) >= 0
    assert int(prom['homelab_compose_up_timestamp_seconds{host="daniel-pi"}']) > 0
    assert mode_of(pi.prom_path) == 0o644
    up = [line for line in pi.log if " up -d" in line]
    assert up and up[0].endswith("--remove-orphans --wait --wait-timeout 730")
    assert [p.name for p in pi.textfiles.iterdir()] == ["homelab_compose.prom"]


def test_the_sha_label_is_the_package_version_and_never_the_manifest(pi, tmp_path):
    manifest = pi.root / "usr/lib/homelab/manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"units": {}}')
    pi.run()
    assert sha_problems(pi.prom(), VERSION.split("+g")[1]) == []

    fixture = mutate(
        RUN,
        tmp_path,
        "sha=$(printf '%s\\n' \"$version\" | sed -n 's/^.*+g\\([0-9a-f]\\{12\\}\\)$/\\1/p')",
        'sha=$(sed -n \'s/.*"sha": "\\(.*\\)".*/\\1/p\' "$root/usr/lib/homelab/manifest.json")',
    )
    pi.run(fixture)
    assert sha_problems(pi.prom(), "0123456789ab") != []


def test_a_version_without_a_git_suffix_writes_unknown(pi):
    pi.version("0.412")

    pi.run()

    assert sha_problems(pi.prom(), "unknown") == []


def test_the_output_never_holds_a_secret_value(pi):
    pi.env({"domain": SECRET})
    good = pi.run()
    pi.service("alloy", "domain", "missing_key", image="grafana/alloy:v1")
    refused = pi.run()

    assert refused.returncode != 0
    for result in (good, refused):
        assert SECRET not in result.stdout + result.stderr


def test_neither_script_calls_systemctl():
    for script in (RUN, ADOPT):
        assert "systemctl" not in script.read_text()
