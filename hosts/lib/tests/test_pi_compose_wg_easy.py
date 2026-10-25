"""daniel-pi's wg-easy runs through the Pi's Compose project (H5.9).

The healthcheck guard moved here with the service from
`ansible/tests/services/test_wg_easy_healthcheck_timing.py`. It reads the rendered package of
`hosts/daniel-pi/package.yml`, which is what the Pi installs, never a template's source text.

- **Healthcheck.** The image ships `wg show | grep -q interface` as its HEALTHCHECK, and its
  Dockerfile puts the timing flags inside the CMD string, so `Config.Healthcheck` carries none.
  A `healthcheck:` with timing and NO `test:` widens the timing and keeps the image's probe
  (dockerd fills `Test` from the image). A `test:` would restate the probe, and a hardcoded
  timing would put wg-easy outside the tunable the other Pi probes read.
- **Reach.** A WireGuard client reaches the Pi on one UDP port, and an operator reaches the
  admin UI on one LAN-bound TCP port. The move keeps both exactly as the role published them.
- **Data.** The WireGuard keys live in `./config`, which pi-peer-backup pulls nightly. The
  bind, and the tmpfiles line that creates it on a rebuilt Pi, must stay.
- **No secret.** wg-easy takes the plain variable `domain` and no SOPS key.

Run: uv run pytest hosts/lib/tests/test_pi_compose_wg_easy.py
"""

import pytest
import yaml
from _compose_tree import COMPOSE, project_of

from lib.estate import Estate
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

TIMING_KEYS = ("interval", "timeout", "retries")
COMPOSE_FILE = f"{COMPOSE}/wg-easy/compose.yaml"
TMPFILES = REPO / "hosts/components/pi-compose/tmpfiles.d/homelab-compose.conf"
PEER_BACKUP_DEFAULTS = REPO / "ansible/roles/k8s/pi-peer-backup/defaults/main.yml"
PROJECT_DIR = "/home/ubuntu/server/containers/wg-easy"
# The ports a client and the operator already use, written out so a drifted inventory entry
# fails here instead of silently moving the endpoint.
UDP_PORT = 51822
UI_PORT = 51821


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-pi")


@pytest.fixture(scope="module")
def service(package) -> dict:
    return project_of(package)["files"][COMPOSE_FILE]["services"]["wg-easy"]


@pytest.fixture(scope="module")
def pi_vars() -> dict:
    return Estate().vars("daniel-pi")


# --- healthcheck ------------------------------------------------------------------------------


def test_timing_comes_from_the_pi_host_vars(service, pi_vars):
    """ACCEPT: the block reads the same three tunables docker-proxy reads."""
    expected = {k: pi_vars[f"container_healthcheck_{k}"] for k in TIMING_KEYS}
    assert {k: service["healthcheck"][k] for k in TIMING_KEYS} == expected


def test_the_image_probe_is_not_restated(service):
    """REJECT a `test:`: that is a redundant probe, and dockerd fills `Test` from the image."""
    hc = service["healthcheck"]
    assert "test" not in hc, f"wg-easy restates the image probe: {hc['test']!r}"
    assert set(hc) == set(TIMING_KEYS), f"unexpected healthcheck keys: {sorted(hc)}"


def test_the_pi_widens_past_dockers_defaults(pi_vars):
    """The point of the block: the Pi's values must exceed the 30s/3 the image ran under."""
    assert int(str(pi_vars["container_healthcheck_timeout"]).rstrip("s")) > 30
    assert int(pi_vars["container_healthcheck_retries"]) > 3


# --- reach ------------------------------------------------------------------------------------


def test_the_wireguard_port_is_published_on_every_interface_as_udp(service):
    assert f"{UDP_PORT}:{UDP_PORT}/udp" in service["ports"]
    assert f"WG_PORT={UDP_PORT}" in service["environment"]


def test_the_admin_ui_is_bound_to_the_lan_address_only(service, pi_vars):
    lan = pi_vars["server_ip"]
    assert f"{lan}:{UI_PORT}:51821/tcp" in service["ports"]
    assert len(service["ports"]) == 2, service["ports"]


def test_the_client_endpoint_is_the_wireguard_hostname(service, pi_vars):
    assert f"WG_HOST=wireguard.{pi_vars['domain']}" in service["environment"]


# --- data -------------------------------------------------------------------------------------


def test_the_keys_directory_is_the_bind_pi_peer_backup_pulls(service):
    """The CronJob reads `<project dir>/config/`; a moved bind would back up nothing."""
    binds = [v for v in service["volumes"] if v["target"] == "/etc/wireguard"]
    assert binds == [
        {
            "type": "bind",
            "source": "./config",
            "target": "/etc/wireguard",
            "bind": {"create_host_path": False},
        }
    ]
    defaults = yaml.safe_load(PEER_BACKUP_DEFAULTS.read_text())
    sys_user = Estate().vars("daniel-pi")["sys_user"]
    backup_src = defaults["pi_peer_backup_src_dir"].replace("{{ sys_user }}", sys_user)
    assert backup_src == f"{PROJECT_DIR}/config/"


def test_the_project_and_config_directories_have_tmpfiles_lines_with_the_live_modes():
    """755 ubuntu for the project directory and 755 root for ./config, read from the live Pi."""
    lines = [
        line.split()
        for line in TMPFILES.read_text().splitlines()
        if line.startswith("d ")
    ]
    assert ["d", PROJECT_DIR, "0755", "ubuntu", "ubuntu", "-"] in lines
    assert ["d", f"{PROJECT_DIR}/config", "0755", "root", "root", "-"] in lines


def test_the_package_points_the_include_at_the_project_directory(package):
    include = next(
        i for i in project_of(package)["includes"] if i["path"] == COMPOSE_FILE
    )
    assert include["project_directory"] == PROJECT_DIR


# --- no secret --------------------------------------------------------------------------------


def test_wg_easy_reads_no_compose_env_key(package, service):
    text_ = project_of(package)["files"][COMPOSE_FILE]
    assert "${" not in str(text_), "wg-easy reads a Compose env file key"
    assert not [e for e in service["environment"] if "PASSWORD_HASH" in e]
