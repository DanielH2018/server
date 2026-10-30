"""The rendered `pi-heartbeats` component: the Pi's three heartbeats as packaged timers (H3.8b).

The scripts' own behaviour is run against stubs in `ansible/tests/setup/test_pi_*.py`. These
tests hold what the move added: the units that run the scripts, the credential each token
script reads, the copied push helper, and the Autonomous-role contract, which moved from
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
# Each token script's unit and the credential it loads from /run/credstore (H4.5).
CREDENTIALS = {
    "homelab-pi-sd-health.service": "pi_sd_health_push_token",
    "homelab-pi-recovery.service": "pi_recovery_push_token",
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
    # H7.26 deleted docker_install's docker-group task, the only thing that added sys_user to
    # `docker`, so the unit carries the grant (#6599).
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


@pytest.mark.parametrize(("service", "key"), sorted(CREDENTIALS.items()))
def test_each_token_script_reads_its_credential_and_embeds_no_token(
    package, service, key
):
    unit = _text(package, f"{UNITS}/{service}")
    assert f"LoadCredential={key}:/run/credstore/{key}\n" in unit
    assert "/etc/homelab/interim" not in unit
    assert "After=homelab-secrets.service\n" in unit
    assert "Wants=homelab-secrets.service\n" in unit
    script = _text(package, unit.split("ExecStart=", 1)[1].split("\n", 1)[0])
    assert f'"${{CREDENTIALS_DIRECTORY:-}}/{key}"' in script
    assert "/api/push/${PUSH_TOKEN}" in script


def test_the_scripts_render_domain_from_inventory(package):
    domain = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["domain"]
    for script in ("pi-sd-health", "pi-recovery-health"):
        assert f'KUMA_HOST="uptime-kuma.local.{domain}"' in _text(
            package, f"{LIB}/{script}"
        )


def test_the_push_helper_is_a_byte_copy_of_initial_setups(package):
    source = REPO / host_mode.SHARED_FILES["kuma-push-lib.sh"]
    assert package.files[f"{LIB}/kuma-push-lib.sh"] == (source.read_bytes(), 0o644)
    for script in ("pi-sd-health", "pi-recovery-health"):
        assert f"source {LIB}/kuma-push-lib.sh" in _text(package, f"{LIB}/{script}")


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


def test_the_package_ships_the_secrets_unit_and_names_both_credentials_it_loads(
    package,
):
    assert "homelab-secrets.service" in package.units()
    assert sorted(package.credentials) == sorted(CREDENTIALS.values())
