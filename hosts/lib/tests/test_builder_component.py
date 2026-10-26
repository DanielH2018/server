"""The rendered `builder` and `failure-alert` components in daniel-box's package (H7.41b).

Until H7.41b the `homelab_builder` role installed the builder, the signer and the apt repository.
These tests hold what the move carries: the sandbox lists of the plan's *The builder and the
repository*, the credential that replaces the token in the settings, the interpreter install and
the one `ExecStart=` that sets `UV_PYTHON_DOWNLOADS=never`, the fingerprint script, the shared
alert unit, the declared users and ufw rules, and the legacy entries that hand the Ansible-era
files to the package.

Run: uv run pytest hosts/lib/tests/test_builder_component.py
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from _checkout_package import render_daniel_box

from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UNITS = "/usr/lib/systemd/system"
BUILDER = f"{UNITS}/homelab-builder.service"
SIGNER = f"{UNITS}/homelab-repo-sign.service"
REPO_UNIT = f"{UNITS}/homelab-apt-repo.service"
TIMER = f"{UNITS}/homelab-builder.timer"
ALERT = f"{UNITS}/homelab-alert@.service"
SETTINGS = "/usr/lib/homelab/builder/builder.conf"
FINGERPRINT = REPO / "hosts" / "lib" / "builder-fingerprint"
PUBLISH = REPO / "hosts" / "lib" / "publish-textfile"

_BOTH = {
    "NoNewPrivileges": ["yes"],
    "ProtectSystem": ["strict"],
    "ProtectHome": ["yes"],
    "PrivateTmp": ["yes"],
    "PrivateDevices": ["yes"],
    "ProtectKernelTunables": ["yes"],
    "ProtectKernelModules": ["yes"],
    "ProtectControlGroups": ["yes"],
    "RestrictSUIDSGID": ["yes"],
    "CapabilityBoundingSet": [""],
}
SANDBOX = {
    BUILDER: {**_BOTH, "ReadWritePaths": ["/var/lib/homelab-builder"]},
    SIGNER: {
        **_BOTH,
        "PrivateNetwork": ["yes"],
        "ReadWritePaths": [
            "/var/lib/homelab-signer",
            "/var/lib/homelab-builder/staged",
        ],
    },
}


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(
        tmp_path_factory.mktemp("builder"), "failure-alert", "builder"
    )


def text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def directives(unit: str) -> dict[str, list[str]]:
    """Each `Key=value` line of a unit, every value of a repeated key in order."""
    found: dict[str, list[str]] = {}
    for line in unit.splitlines():
        if "=" in line and not line.startswith(("#", ";", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def sandbox_problems(unit: str, wanted: dict[str, list[str]]) -> list[str]:
    got = directives(unit)
    return [
        f"{key}={got.get(key)} where the plan names {value}"
        for key, value in wanted.items()
        if got.get(key) != value
    ]


@pytest.mark.parametrize("dst", sorted(SANDBOX))
def test_each_unit_carries_the_plans_sandbox(package, dst):
    assert sandbox_problems(text(package, dst), SANDBOX[dst]) == []


def test_a_builder_unit_without_no_new_privileges_is_flagged(package):
    unit = text(package, BUILDER).replace("NoNewPrivileges=yes\n", "")
    assert sandbox_problems(unit, SANDBOX[BUILDER]) == [
        "NoNewPrivileges=None where the plan names ['yes']"
    ]


def test_a_signer_with_a_network_or_a_second_writable_path_is_flagged(package):
    unit = text(package, SIGNER).replace("PrivateNetwork=yes\n", "")
    assert sandbox_problems(unit, SANDBOX[SIGNER])
    assert sandbox_problems(unit + "ReadWritePaths=/etc\n", SANDBOX[SIGNER])


def test_both_units_keep_a_state_directory_at_0711_and_log_at_notice(package):
    # fact: hosts/components/builder/CLAUDE.md#The builder's run
    for dst, name in ((BUILDER, "homelab-builder"), (SIGNER, "homelab-signer")):
        got = directives(text(package, dst))
        assert got["StateDirectory"] == [name]
        assert got["StateDirectoryMode"] == ["0711"]
        # This host's journald stores notice and above, so a lower level loses a traceback.
        assert got["SyslogLevel"] == ["notice"]


def test_a_unit_left_at_the_default_log_level_is_flagged(package):
    unit = text(package, BUILDER).replace("SyslogLevel=notice\n", "")
    assert directives(unit).get("SyslogLevel") is None


def test_both_oneshots_page_through_the_shared_alert_template(package):
    builder = directives(text(package, BUILDER))
    signer = directives(text(package, SIGNER))
    assert builder["User"] == ["homelab-builder"]
    assert signer["User"] == ["homelab-signer"]
    assert builder["OnFailure"] == signer["OnFailure"] == ["homelab-alert@%n.service"]
    assert ALERT in package.files


def test_every_successful_builder_run_starts_the_signer_and_waits_for_a_running_one(
    package,
):
    builder = directives(text(package, BUILDER))
    assert builder["OnSuccess"] == ["homelab-repo-sign.service"]
    assert "homelab-repo-sign.service" in builder["After"]
    signer = text(package, SIGNER)
    # The reverse After= would be an ordering cycle, and a timer of its own would sign
    # between a builder run's writes.
    assert "homelab-builder.service" not in " ".join(
        directives(signer).get("After", [])
    )
    assert f"{UNITS}/homelab-repo-sign.timer" not in package.files
    assert "\n[Install]" not in signer


def test_the_timer_fires_every_two_minutes_and_is_enabled(package):
    assert directives(text(package, TIMER))["OnUnitActiveSec"] == ["2min"]
    assert package.links[f"{UNITS}/timers.target.wants/homelab-builder.timer"] == (
        "../homelab-builder.timer"
    )
    assert package.links[f"{UNITS}/multi-user.target.wants/homelab-apt-repo.service"]


def test_the_token_is_a_credential_and_the_settings_hold_none(package):
    got = directives(text(package, BUILDER))
    assert got["LoadCredential"] == [
        "github_token:/run/credstore/homelab_builder_github_token"
    ]
    assert "homelab_builder_github_token" in package.credentials
    assert "GITHUB_TOKEN" not in text(package, SETTINGS)


def test_the_interpreter_installs_in_exec_start_pre_and_only_exec_start_forbids_downloads(
    package,
):
    unit = text(package, BUILDER)
    got = directives(unit)
    version = yaml_fast.safe_load(_group_vars())["host_python_version"]
    pre = got["ExecStartPre"]
    assert pre[-1] == f"/usr/lib/homelab/bin/uv python install --no-bin {version}"
    assert not any("UV_PYTHON_DOWNLOADS" in line for line in pre)
    assert not any("UV_PYTHON_DOWNLOADS" in line for line in got["Environment"])
    [start] = got["ExecStart"]
    assert start.startswith(
        "/usr/bin/env UV_PYTHON_DOWNLOADS=never /usr/lib/homelab/bin/uv run "
    )
    assert "--no-python-downloads" in start
    env = dict(line.split("=", 1) for line in got["Environment"])
    assert env["HOMELAB_UV"] == "/usr/lib/homelab/bin/uv"
    assert env["HOMELAB_NFPM"] == "/usr/lib/homelab/bin/nfpm"
    assert env["HOMELAB_PYTHON"] == version
    assert env["UV_PYTHON_INSTALL_DIR"].startswith(env["HOME"])
    assert env["UV_CACHE_DIR"].startswith(env["HOME"])


def _group_vars() -> str:
    return (REPO / "ansible/inventory/group_vars/all.yml").read_text()


def test_a_unit_that_sets_the_download_ban_for_the_install_too_is_flagged(package):
    unit = text(package, BUILDER) + "Environment=UV_PYTHON_DOWNLOADS=never\n"
    assert any("UV_PYTHON_DOWNLOADS" in v for v in directives(unit)["Environment"])


def test_the_fingerprint_script_runs_as_root_before_the_builder(package):
    pre = directives(text(package, BUILDER))["ExecStartPre"]
    assert "+/usr/lib/homelab/builder-fingerprint" in pre
    assert pre.index("+/usr/lib/homelab/builder-fingerprint") < len(pre) - 1
    # The red fixture drops the line, and the builder would then refuse with a key present.
    stripped = text(package, BUILDER).replace(
        "ExecStartPre=+/usr/lib/homelab/builder-fingerprint\n", ""
    )
    assert (
        "+/usr/lib/homelab/builder-fingerprint"
        not in directives(stripped)["ExecStartPre"]
    )


def test_both_units_publish_their_own_series_as_root(package):
    for dst, prefix in ((BUILDER, "homelab_builder_"), (SIGNER, "homelab_repo_")):
        [post] = directives(text(package, dst))["ExecStopPost"]
        assert post.startswith(f"-+/bin/sh /usr/lib/homelab/publish-textfile {prefix} ")


def test_the_settings_carry_the_deployers_ci_contexts_and_two_prometheus_urls(package):
    # fact: hosts/components/builder/CLAUDE.md#The rollout gate (H3.12)
    # fact: hosts/components/builder/CLAUDE.md#What the component installs
    settings = dict(
        line.split("=", 1)
        for line in text(package, SETTINGS).splitlines()
        if line and not line.startswith("#")
    )
    groups = yaml_fast.safe_load(_group_vars())
    assert settings["CI_CONTEXTS"] == ",".join(groups["ci_required_contexts"])
    assert settings["CI_ANCESTOR_WALK_MAX"] == str(groups["ci_ancestor_walk_max"])
    observability = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/observability/defaults/main.yml").read_text()
    )
    ip = observability["observability_prometheus_cluster_ip"]
    assert settings["PROMETHEUS_URLS"] == f"http://127.0.0.1:9090 http://{ip}:9090"


def test_the_nfpm_pin_is_ci_s_and_uv_matches_initial_setup():
    # fact: hosts/components/builder/CLAUDE.md#What the component installs
    ci = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    env = ci["jobs"]["host_packages_build"]["env"]
    tools = yaml_fast.safe_load(
        (REPO / "hosts/packages/homelab-tools/package.yml").read_text()
    )["tools"]
    assert tools["nfpm"]["amd64"]["version"] == env["NFPM_VERSION"]
    assert tools["nfpm"]["amd64"]["sha256"] == env["NFPM_SHA256"]
    defaults = yaml_fast.safe_load(
        (REPO / "ansible/roles/setup/initial_setup/defaults/main.yml").read_text()
    )
    assert tools["uv"]["amd64"]["version"] == defaults["initial_setup_uv_version"]


def test_the_alert_template_pages_with_the_instance_name_and_a_credential(package):
    unit = text(package, ALERT)
    got = directives(unit)
    assert got["LoadCredential"] == [
        "discord_webhook:/run/credstore/gitops_deploy_discord_webhook"
    ]
    assert "gitops_deploy_discord_webhook" in package.credentials
    [start] = got["ExecStart"][:1]
    assert "'instance=%i'" in unit and "{{instance:json}}" in unit
    assert "--expand-url '{{webhook:trim}}'" in unit
    assert "/bin/sh" not in start and "http" not in unit.split("ExecStart=")[1]
    assert got["Restart"] == ["on-failure"] and got["RestartPreventExitStatus"] == ["3"]
    assert got["SyslogLevel"] == ["notice"]


def test_users_are_declared_by_uid_alone(package):
    assert {u["name"]: u["uid"] for u in package.users} == {
        "homelab-builder": 995,
        "homelab-signer": 994,
    }
    assert all("gid" not in u for u in package.users)


def test_each_ansible_era_ufw_rule_is_replaced_by_a_declared_rule(package):
    declared = {r["id"]: r["spec"] for r in package.ufw_rules}
    assert set(declared) == {"apt-repo-daniel-server", "apt-repo-daniel-pi"}
    assert all(
        r["spec"].endswith("to any port 8791 proto tcp") for r in package.ufw_rules
    )
    assert len(package.ufw_legacy) == 2
    for entry in package.ufw_legacy:
        assert entry["comment"] == "homelab apt repository"
        assert declared[entry["replaced_by"]] == entry["spec"]
    # A quote would fail the grammar, which is why the comment sits in its own key.
    assert not any("'" in r["spec"] for r in package.ufw_rules)


def test_every_ansible_era_unit_file_and_link_is_a_legacy_path_the_package_ships(
    package,
):
    paths = {e["path"] for e in package.legacy if "path" in e}
    etc = "/etc/systemd/system"
    shadowed = {p for p in paths if p.startswith(etc)}
    assert shadowed == {
        f"{etc}/homelab-builder.service",
        f"{etc}/homelab-builder.timer",
        f"{etc}/homelab-repo-sign.service",
        f"{etc}/homelab-apt-repo.service",
        f"{etc}/timers.target.wants/homelab-builder.timer",
        f"{etc}/multi-user.target.wants/homelab-apt-repo.service",
    }
    shipped = {Path(dst).name for dst in package.files} | {
        Path(dst).name for dst in package.links
    }
    assert {Path(p).name for p in shadowed} <= shipped
    # The package's own links live under /usr/lib, so the handoff cannot touch them.
    assert not any(dst.startswith(etc) for dst in package.links)
    units = {e["unit"] for e in package.legacy if "unit" in e}
    assert units == {"homelab-builder-alert.service", "homelab-repo-sign-alert.service"}
    assert {"/etc/homelab-builder/alert-webhook.env"} <= paths


def test_the_tmpfiles_file_gives_the_directories_the_roles_modes(package):
    rows = {
        line.split()[1]: line.split()[2:5]
        for line in text(
            package, "/usr/lib/tmpfiles.d/homelab-builder.conf"
        ).splitlines()
        if line.startswith("d ")
    }
    assert rows["/var/lib/homelab-builder/staged"] == [
        "2770",
        "homelab-builder",
        "homelab-signer",
    ]
    assert rows["/var/lib/homelab-signer/gnupg"][0] == "0700"
    assert rows["/var/lib/homelab-signer/repo"][0] == "0755"
    assert rows["/var/lib/homelab-builder"][0] == "0711"


def test_the_repository_listens_on_the_hosts_address(package):
    exec_start = directives(text(package, REPO_UNIT))["ExecStart"][0]
    assert re.search(r"--bind \d+\.\d+\.\d+\.\d+ 8791 ", exec_start)
    assert exec_start.endswith("--directory /var/lib/homelab-signer/repo")


# --- the fingerprint script ------------------------------------------------------------------


def gpg(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--pinentry-mode",
            "loopback",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def make_key(home: Path, uid: str) -> str:
    gpg(
        home,
        "--passphrase",
        "",
        "--quick-generate-key",
        uid,
        "ed25519",
        "sign",
        "never",
    )
    listing = gpg(home, "--with-colons", "--list-keys", uid).stdout
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
        check=False,
        timeout=30,
    )
    return next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )


def fingerprint(tmp_path: Path, home: Path, out: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOMELAB_SIGNER_GNUPG": str(home),
        "HOMELAB_FPR_FILE": str(out),
        "HOMELAB_RUNAS": "",
    }
    return subprocess.run(
        ["/bin/sh", str(FINGERPRINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def signer_home(tmp_path) -> Path:
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    return home


def test_the_fingerprint_script_writes_the_key_at_0644(tmp_path, signer_home):
    key = make_key(signer_home, "a <a@example.invalid>")
    out = tmp_path / "archive-key.fpr"
    result = fingerprint(tmp_path, signer_home, out)
    assert result.returncode == 0, result.stderr
    assert out.read_text() == key + "\n"
    assert out.stat().st_mode & 0o777 == 0o644
    assert [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".archive-key")
    ] == []


def test_a_home_with_no_key_leaves_no_file_and_exits_0(tmp_path, signer_home):
    out = tmp_path / "archive-key.fpr"
    out.write_text("STALE\n")
    result = fingerprint(tmp_path, signer_home, out)
    assert result.returncode == 0, result.stderr
    assert not out.exists(), (
        "a stale fingerprint would let the builder trust an old key"
    )


def test_a_home_with_two_keys_fails_and_keeps_the_file(tmp_path, signer_home):
    make_key(signer_home, "a <a@example.invalid>")
    make_key(signer_home, "b <b@example.invalid>")
    out = tmp_path / "archive-key.fpr"
    out.write_text("KEEP\n")
    result = fingerprint(tmp_path, signer_home, out)
    assert result.returncode == 1
    assert out.read_text() == "KEEP\n"


# --- publish-textfile ------------------------------------------------------------------------


def publish(tmp_path: Path, source: Path, *, textfile: Path | None = None):
    textfile = textfile or tmp_path / "textfile"
    return subprocess.run(
        ["/bin/sh", str(PUBLISH), "homelab_builder_", str(source)],
        env={**os.environ, "TEXTFILE_DIR": str(textfile)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_publish_textfile_copies_only_the_units_series_at_0644(tmp_path):
    (tmp_path / "textfile").mkdir()
    source = tmp_path / "homelab_builder.prom"
    source.write_text(
        "# TYPE homelab_builder_fetch_timestamp_seconds gauge\n"
        "homelab_builder_fetch_timestamp_seconds 1\n"
        'homelab_builder_unbuilt_since_timestamp_seconds{reason="ok",sha="ab"} 0\n'
        "homelab_repo_published_info 1\n"
        "node_load1 9\n"
    )
    assert publish(tmp_path, source).returncode == 0
    out = tmp_path / "textfile" / "homelab_builder.prom"
    assert out.read_text().splitlines() == [
        "# TYPE homelab_builder_fetch_timestamp_seconds gauge",
        "homelab_builder_fetch_timestamp_seconds 1",
        'homelab_builder_unbuilt_since_timestamp_seconds{reason="ok",sha="ab"} 0',
    ]
    assert out.stat().st_mode & 0o777 == 0o644


def test_publish_textfile_refuses_a_symlink_and_a_missing_directory(tmp_path):
    (tmp_path / "textfile").mkdir()
    link = tmp_path / "link.prom"
    link.symlink_to("/etc/hostname")
    assert publish(tmp_path, link).returncode == 1
    source = tmp_path / "homelab_builder.prom"
    source.write_text("homelab_builder_fetch_timestamp_seconds 1\n")
    assert publish(tmp_path, source, textfile=tmp_path / "absent").returncode == 1
    assert not (tmp_path / "absent").exists(), "it created the directory"
