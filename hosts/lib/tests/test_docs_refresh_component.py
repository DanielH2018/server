"""The rendered `docs-refresh` component: the generated-docs refresh in daniel-box's package (H7.10a).

Until H7.36 `initial_setup` installed the refresh as a cron and `k8s_host_files` created the docs
site directory. These tests hold what the move carries: the credential that replaces the token
rendered into the script, the `+` ExecStartPre that creates the directories as root for a service
that runs as `sys_user`, the tmpfiles line that equals docs' `docs_host_site_dir`, the timer's
slots, and the legacy entries.

Run: uv run pytest hosts/lib/tests/test_docs_refresh_component.py
"""

import re

import pytest
from _checkout_package import render_daniel_box

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "docs-refresh" / "component.yml"
UNITS = "/usr/lib/systemd/system"
SCRIPT = "/usr/lib/homelab/docs-refresh"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-docs-site.conf"
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope:",
    "Mode:",
    "Abort valves:",
    "Authoritative sources:",
    "Required evidence:",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(tmp_path_factory.mktemp("docs-refresh"), "docs-refresh")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _user() -> str:
    return render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]


def tmpfiles_paths(text: str) -> dict[str, str]:
    """{path: mode} for each `d` line of a tmpfiles.d file."""
    rows = (line.split() for line in text.splitlines() if line.startswith("d "))
    return {row[1]: row[2] for row in rows}


def site_dir_problems(tmpfiles: str, docs_default: str, user: str) -> list[str]:
    """Why the tmpfiles file does not create the directory docs mounts, [] when it does."""
    expected = docs_default.replace("{{ sys_user }}", user)
    paths = tmpfiles_paths(tmpfiles)
    if expected not in paths:
        return [f"no `d` line for {expected}; the file creates {sorted(paths)}"]
    if paths[expected] != "0755":
        return [f"{expected} has mode {paths[expected]}, want 0755"]
    return []


def test_the_script_and_tmpfiles_install_and_the_timer_is_linked(package):
    assert package.files[SCRIPT][1] == 0o755
    assert TMPFILES in package.files
    assert "homelab-docs-refresh.timer" in package.enable_links()


def test_the_service_creates_directories_as_root_and_runs_as_sys_user(package):
    unit = _text(package, f"{UNITS}/homelab-docs-refresh.service")
    assert f"User={_user()}\n" in unit
    assert f"ExecStartPre=+/usr/bin/systemd-tmpfiles --create {TMPFILES}\n" in unit
    assert f"ExecStart={SCRIPT}\n" in unit
    assert "ProtectHome" not in unit


def test_the_token_is_a_credential_and_never_in_the_script(package):
    unit = _text(package, f"{UNITS}/homelab-docs-refresh.service")
    assert (
        "LoadCredential=docs_refresh_push_token:/run/credstore/docs_refresh_push_token\n"
        in unit
    )
    assert "Wants=homelab-secrets.service\n" in unit
    spec = yaml_fast.safe_load(COMPONENT.read_text())
    assert spec["credentials"] == ["docs_refresh_push_token"]
    assert "CREDENTIALS_DIRECTORY" in _text(package, SCRIPT)


def test_the_tmpfiles_file_creates_the_directory_docs_mounts_and_the_stamp_directory(
    package,
):
    docs = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/docs/defaults/main.yml").read_text()
    )
    tmpfiles = _text(package, TMPFILES)
    assert site_dir_problems(tmpfiles, docs["docs_host_site_dir"], _user()) == []
    assert "/var/lib/homelab/docs-refresh.d" in tmpfiles_paths(tmpfiles)
    # The script's SITE_DIR is a third copy of the same path.
    script = _text(package, SCRIPT)
    assert f'SITE_DIR="/home/{_user()}/docs-site"' in script


def test_a_tmpfiles_file_for_another_directory_is_flagged(package):
    wrong = _text(package, TMPFILES).replace("docs-site", "elsewhere")
    assert site_dir_problems(wrong, "/home/{{ sys_user }}/docs-site", _user()) == [
        f"no `d` line for /home/{_user()}/docs-site; the file creates "
        f"{sorted(tmpfiles_paths(wrong))}"
    ]


def test_the_timer_keeps_the_crons_two_slots(package):
    timer = _text(package, f"{UNITS}/homelab-docs-refresh.timer")
    assert re.search(r"^OnCalendar=\*-\*-\* 06,18:17:00$", timer, re.M)


def test_the_package_retires_the_cron_file_and_the_script(package):
    legacy = [entry for entry in package.legacy if "docs-refresh" in str(entry)]
    assert {"cron_file": "docs-refresh"} in legacy
    assert {"path": "/usr/local/bin/docs-refresh.sh"} in legacy


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
