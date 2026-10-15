"""The sops CI installs is the one the hosts install, by version and by digest.

CI's `pytest` job installs sops for `cluster_vars.py write`'s tests (target slice D3), whose
whole point is the pinned binary's behaviour on a dotenv file. The hosts' pin is
`sops_setup_version` and `sops_setup_binary_amd64_sha256` in
`ansible/roles/setup/sops_setup/defaults/main.yml`, and the step's env holds a second copy of
both. Renovate's annotation manager reads the defaults alone, so a sops bump leaves the CI
copy behind, and the tests would then pass against a binary no host runs. This guard is what
fails that bump until the step's `SOPS_VERSION` and `SOPS_SHA256` are moved too.

Sibling to `test_vale_matches_the_ci_pin.py`, one module per twin class.

Run: uv run pytest ansible/tests/repo/test_ci_sops_matches_the_host_pin.py
"""

from lib import yaml_fast
from _helpers import REPO

CI_REL = ".github/workflows/ci.yml"
DEFAULTS_REL = "ansible/roles/setup/sops_setup/defaults/main.yml"
STEP = "Install sops and age for the cluster_vars tests"


def ci_pin(workflow: dict) -> tuple[str, str] | None:
    """(version, sha256) from the pytest job's install step, or None when it is gone."""
    for step in workflow["jobs"]["pytest"]["steps"]:
        if step.get("name") == STEP:
            env = step.get("env") or {}
            return str(env.get("SOPS_VERSION")), str(env.get("SOPS_SHA256"))
    return None


def host_pin(defaults: dict) -> tuple[str, str]:
    return (
        str(defaults["sops_setup_version"]),
        str(defaults["sops_setup_binary_amd64_sha256"]),
    )


def pin_problems(ci: tuple[str, str] | None, host: tuple[str, str]) -> list[str]:
    if ci is None:
        return [f"{CI_REL} has no `{STEP}` step in the pytest job"]
    return [
        f"CI's {name} is {ci_value}, the hosts' is {host_value}"
        for name, ci_value, host_value in zip(
            ("version", "sha256"), ci, host, strict=True
        )
        if ci_value != host_value
    ]


def test_a_matching_pin_is_clean():
    assert pin_problems(("v3.13.3", "ab"), ("v3.13.3", "ab")) == []


def test_a_version_left_behind_is_flagged():
    assert pin_problems(("v3.13.3", "ab"), ("v3.14.0", "ab")) == [
        "CI's version is v3.13.3, the hosts' is v3.14.0"
    ]


def test_a_digest_left_behind_is_flagged():
    assert pin_problems(("v3.14.0", "ab"), ("v3.14.0", "cd"))


def test_a_missing_step_is_flagged():
    assert pin_problems(None, ("v3.13.3", "ab"))


def test_ci_installs_the_sops_the_hosts_install():
    workflow = yaml_fast.safe_load((REPO / CI_REL).read_text())
    defaults = yaml_fast.safe_load((REPO / DEFAULTS_REL).read_text())
    problems = pin_problems(ci_pin(workflow), host_pin(defaults))
    assert problems == [], (
        f"{problems}. Move the step's SOPS_VERSION and SOPS_SHA256 in {CI_REL} to the "
        f"sops_setup pin in {DEFAULTS_REL}"
    )
