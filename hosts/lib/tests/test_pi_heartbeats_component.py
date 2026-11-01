"""The rendered `pi-heartbeats` component: the Pi's three heartbeats as packaged timers (H3.8b).

The scripts' own behaviour is run against stubs in `ansible/tests/setup/test_pi_*.py`. These
tests hold what the move added: the units that run the scripts, the absence of any credential
or push in the two token heartbeats, the copied push helper the Pi's scans still source, and the
Autonomous-role contract, which moved from
`optimize_pi`'s `CLAUDE.md` into `component.yml`'s header with the recovery heartbeat.

Run: uv run pytest hosts/lib/tests/test_pi_heartbeats_component.py
"""

import re

import pytest

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "pi-heartbeats" / "component.yml"
UNITS = "/usr/lib/systemd/system"
LIB = "/usr/lib/homelab"
CONTRACT_HEADING = "# Autonomous-role contract"
# The parts of the contract a reader acts on, as `optimize_pi`'s section carried them.
CONTRACT_PARTS = (
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Required evidence, every cycle:",
    "Next-run review:",
)
# The two units whose scripts pushed to Kuma until M6 batch B5's Cutover-2, and their scripts.
TOKEN_SERVICES = {
    "homelab-pi-sd-health.service": "pi-sd-health",
    "homelab-pi-recovery.service": "pi-recovery-health",
}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def contract_problems(text: str) -> list[str]:
    """What the component's header lacks of the contract, [] when it carries all of it."""
    header = [line for line in text.splitlines() if line.startswith("#")]
    if CONTRACT_HEADING not in "\n".join(header):
        return [f"no `{CONTRACT_HEADING}` in the header comment"]
    return [
        f"no `{part}`"
        for part in CONTRACT_PARTS
        if not any(part in line for line in header)
    ]


def test_the_component_header_carries_the_contract_is_clean():
    assert contract_problems(COMPONENT.read_text()) == []


def test_a_header_without_the_contract_is_flagged():
    stripped = "\n".join(
        line
        for line in COMPONENT.read_text().splitlines()
        if "contract" not in line.lower()
    )
    assert contract_problems(stripped) == [
        f"no `{CONTRACT_HEADING}` in the header comment"
    ]


def test_the_component_ships_six_units_and_links_its_timers(package):
    names = ("sd-health", "gz-integrity", "recovery")
    units = {
        f"homelab-pi-{name}.{kind}" for name in names for kind in ("service", "timer")
    }
    assert units <= set(package.units())
    assert set(package.enable_links()) >= {f"homelab-pi-{name}.timer" for name in names}


def test_each_service_runs_its_script_as_sys_user(package):
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    for service, script in (
        ("homelab-pi-sd-health.service", "pi-sd-health"),
        ("homelab-pi-gz-integrity.service", "pi-gz-integrity"),
        ("homelab-pi-recovery.service", "pi-recovery-health"),
    ):
        unit = _text(package, f"{UNITS}/{service}")
        assert f"User={user}\n" in unit, service
        assert f"ExecStart={LIB}/{script}\n" in unit, service
        assert "LogsDirectory=pi-health\n" in unit, service
        assert package.files[f"{LIB}/{script}"][1] == 0o755


def _runs_docker(script: str) -> bool:
    """Whether a script invokes the docker CLI, ignoring comments."""
    return any(
        re.search(r"(^|[\s(\"`$])docker\s", line.split("#", 1)[0])
        for line in script.splitlines()
    )


def test_each_service_that_runs_docker_grants_the_docker_group(package):
    # No task adds sys_user to `docker`, so each unit that needs the group grants it.
    runs_docker = []
    for name in package.units():
        if not name.startswith("homelab-pi-") or not name.endswith(".service"):
            continue
        unit = _text(package, f"{UNITS}/{name}")
        script = _text(package, unit.split("ExecStart=", 1)[1].split("\n", 1)[0])
        if _runs_docker(script):
            runs_docker.append(name)
            assert "SupplementaryGroups=docker\n" in unit, name
    assert "homelab-pi-recovery.service" in runs_docker


def credential_problems(unit: str, script: str) -> list[str]:
    """Why a heartbeat unit and its script still depend on a push credential, or nothing."""
    problems = []
    if "LoadCredential=" in unit:
        problems.append("the unit loads a credential")
    if "homelab-secrets.service" in unit:
        problems.append("the unit waits for homelab-secrets.service")
    if "CREDENTIALS_DIRECTORY" in script:
        problems.append("the script reads $CREDENTIALS_DIRECTORY")
    return problems


@pytest.mark.parametrize("service", sorted(TOKEN_SERVICES))
def test_a_heartbeat_unit_loads_no_credential_and_its_script_reads_none(
    package, service
):
    unit = _text(package, f"{UNITS}/{service}")
    script = _text(package, f"{LIB}/{TOKEN_SERVICES[service]}")
    assert credential_problems(unit, script) == []
    assert "/etc/homelab/interim" not in unit


def test_credential_problems_is_flagged_on_a_unit_that_still_loads_one():
    unit = (
        "After=homelab-secrets.service\nLoadCredential=pi_sd_health_push_token:/run/x\n"
    )
    assert credential_problems(unit, '"${CREDENTIALS_DIRECTORY:-}/x"') == [
        "the unit loads a credential",
        "the unit waits for homelab-secrets.service",
        "the script reads $CREDENTIALS_DIRECTORY",
    ]


def test_the_push_helper_is_a_byte_copy_the_scans_still_source(package):
    source = REPO / host_mode.SHARED_FILES["kuma-push-lib.sh"]
    assert package.files[f"{LIB}/kuma-push-lib.sh"] == (source.read_bytes(), 0o644)
    # The heartbeats stopped sourcing it in Cutover-2; the AIDE and rkhunter scans did not.
    for script in ("pi-sd-health", "pi-recovery-health"):
        assert "kuma-push-lib.sh" not in _text(package, f"{LIB}/{script}")
    for script in ("aide-check", "rkhunter-check"):
        assert f"{LIB}/kuma-push-lib.sh" in _text(package, f"{LIB}/{script}")


def test_the_check_library_is_a_byte_copy_of_initial_setups(package):
    # M6 B5: the two token scripts write their textfile verdict through it.
    source = REPO / host_mode.SHARED_FILES["homelab-check-lib.sh"]
    assert package.files[f"{LIB}/homelab-check-lib.sh"] == (source.read_bytes(), 0o644)
    for script in ("pi-sd-health", "pi-recovery-health"):
        assert f"source {LIB}/homelab-check-lib.sh" in _text(package, f"{LIB}/{script}")


def test_the_integrity_timer_primes_its_verdict_on_start(package):
    # The SD-health heartbeat reads a missing verdict as a down, so the sweep runs a minute
    # after its timer starts rather than first at 01:40.
    assert "OnActiveSec=1min\n" in _text(
        package, f"{UNITS}/homelab-pi-gz-integrity.timer"
    )


def test_the_logrotate_file_ships_at_optimize_pis_path(package):
    text, mode = package.files["/etc/logrotate.d/pi-health"]
    assert mode == 0o644
    assert b"/var/log/pi-health/*.log {" in text
    assert b"create 0644" in text and b"copytruncate" not in text.split(b"{", 1)[1]


def test_the_retired_crons_and_scripts_are_legacy_entries(package):
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    crons = (
        "Pi SD-card health heartbeat",
        "Pi rotated-log integrity sweep",
        "Pi container-recovery heartbeat",
    )
    scripts = ("pi-sd-health", "pi-gz-integrity", "pi-recovery-health")
    expected = [{"cron": name, "user": user} for name in crons] + [
        {"path": f"/usr/local/bin/{name}.sh"} for name in scripts
    ]
    assert all(entry in package.legacy for entry in expected), package.legacy


def test_the_package_keeps_both_push_credentials_until_the_token_pr(package):
    # No unit loads them since Cutover-2, but `host_secrets.py check` pins this list to the Pi's
    # SOPS file, so B5's token PR removes both keys from the list and the file in one step.
    assert "homelab-secrets.service" in package.units()
    assert {"pi_sd_health_push_token", "pi_recovery_push_token"} <= set(
        package.credentials
    )
