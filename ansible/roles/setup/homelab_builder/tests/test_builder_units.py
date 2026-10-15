"""The builder's rendered units, its settings file, its tool pins and publish-textfile.

The sandboxing directives are the list in docs/target-architecture/host/h3-pi-pilot.md, *The
builder and the repository*, Sandboxing: both units' shared list, then each unit's own, the
signer's `PrivateNetwork=yes` among them.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_builder_units.py
"""

import os
import subprocess
from pathlib import Path

import pytest
from _helpers import SETUP_ROLES, load_defaults, load_tasks, render_expr, task_named
from _setup_render import render_setup_text

from lib import yaml_fast
from lib.repo_paths import REPO

ROLE = SETUP_ROLES / "homelab_builder"

DEFAULTS = load_defaults(ROLE)
GITOPS_DEFAULTS = load_defaults(SETUP_ROLES / "gitops_deploy")
BUILDER_TASKS = load_tasks(ROLE / "tasks" / "builder.yml")
PUBLISH = ROLE / "files" / "publish-textfile.sh"

# Every directive both units set, then each unit's own.
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
    "homelab-builder.service.j2": {
        **_BOTH,
        "ReadWritePaths": ["/var/lib/homelab-builder"],
    },
    "homelab-repo-sign.service.j2": {
        **_BOTH,
        "PrivateNetwork": ["yes"],
        "ReadWritePaths": [
            "/var/lib/homelab-signer",
            "/var/lib/homelab-builder/staged",
        ],
    },
}


def directives(text: str) -> dict[str, list[str]]:
    """Each `Key=value` line of a unit, every value of a repeated key in order."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", ";", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def sandbox_problems(text: str, wanted: dict[str, list[str]]) -> list[str]:
    got = directives(text)
    return [
        f"{key}={got.get(key)} where the plan names {value}"
        for key, value in wanted.items()
        if got.get(key) != value
    ]


def render(template: str, **overrides) -> str:
    return render_setup_text("homelab_builder", template, overrides)


@pytest.mark.parametrize("template", sorted(SANDBOX))
def test_each_unit_carries_the_plans_sandbox_is_clean(template):
    assert sandbox_problems(render(template), SANDBOX[template]) == []


def test_a_builder_unit_without_no_new_privileges_is_flagged():
    text = render("homelab-builder.service.j2").replace("NoNewPrivileges=yes\n", "")
    assert sandbox_problems(text, SANDBOX["homelab-builder.service.j2"]) == [
        "NoNewPrivileges=None where the plan names ['yes']"
    ]


def test_a_signer_unit_without_private_network_is_flagged():
    text = render("homelab-repo-sign.service.j2").replace("PrivateNetwork=yes\n", "")
    assert sandbox_problems(text, SANDBOX["homelab-repo-sign.service.j2"]) == [
        "PrivateNetwork=None where the plan names ['yes']"
    ]


def test_a_second_writable_path_is_flagged():
    text = render("homelab-builder.service.j2") + "ReadWritePaths=/etc\n"
    assert sandbox_problems(text, SANDBOX["homelab-builder.service.j2"])


def test_the_builder_runs_as_its_user_and_pages_on_failure():
    got = directives(render("homelab-builder.service.j2"))
    assert got["User"] == ["homelab-builder"]
    assert got["OnFailure"] == ["homelab-builder-alert.service"]


def test_every_successful_builder_run_starts_the_signer_and_waits_for_a_running_one():
    builder = directives(render("homelab-builder.service.j2"))
    assert builder["OnSuccess"] == ["homelab-repo-sign.service"]
    assert "homelab-repo-sign.service" in builder["After"]
    signer = directives(render("homelab-repo-sign.service.j2"))
    assert signer["User"] == ["homelab-signer"]
    assert signer["OnFailure"] == ["homelab-repo-sign-alert.service"]
    # The reverse After= would be an ordering cycle, and a timer of its own would sign
    # between a builder run's writes.
    assert "homelab-builder.service" not in " ".join(signer.get("After", []))
    assert not (ROLE / "templates" / "homelab-repo-sign.timer.j2").exists()


@pytest.mark.parametrize(
    ("template", "prefix", "prom"),
    [
        (
            "homelab-builder.service.j2",
            "homelab_builder_",
            "/var/lib/homelab-builder/homelab_builder.prom",
        ),
        (
            "homelab-repo-sign.service.j2",
            "homelab_repo_",
            "/var/lib/homelab-signer/homelab_repo.prom",
        ),
    ],
)
def test_publish_textfile_runs_as_root_outside_the_sandbox_from_root_owned_code(
    template, prefix, prom
):
    [line] = directives(render(template))["ExecStopPost"]
    assert line == f"-+/bin/sh /opt/homelab-builder/publish-textfile {prefix} {prom}"
    # The root line runs nothing the builder could rewrite.
    assert not DEFAULTS["homelab_builder_code_dir"].startswith(
        DEFAULTS["homelab_builder_state_dir"]
    )


def test_the_timer_fires_every_two_minutes():
    got = directives(render("homelab-builder.timer.j2"))
    assert got["OnUnitActiveSec"] == ["2min"]


def _settings(**gitops_overrides) -> dict[str, str]:
    """The settings file rendered as the task renders it: its `vars:` over the deployer's defaults."""
    gitops = {**GITOPS_DEFAULTS, **gitops_overrides}
    task = task_named(BUILDER_TASKS, "Write the builder's configuration")
    task_vars = {k: render_expr(v, **gitops) for k, v in task["vars"].items()}
    text = render("config.env.j2", **task_vars)
    return {k: v[0] for k, v in directives(text).items()}


def ci_contexts_problem(settings: dict[str, str]) -> str | None:
    """None when CI_CONTEXTS is the deployer's list, read from gitops_deploy's defaults file."""
    want = ",".join(GITOPS_DEFAULTS["gitops_deploy_ci_contexts"])
    got = settings.get("CI_CONTEXTS")
    return (
        None if got == want else f"CI_CONTEXTS={got!r}, the deployer's list is {want!r}"
    )


def test_the_settings_carry_the_deployers_ci_contexts_is_clean():
    settings = _settings()
    assert ci_contexts_problem(settings) is None
    assert settings["CI_CONTEXTS"].startswith("prek ")
    assert settings["CI_ANCESTOR_WALK_MAX"] == str(
        GITOPS_DEFAULTS["gitops_deploy_ci_ancestor_walk_max"]
    )


def test_a_settings_render_from_another_list_is_flagged():
    assert ci_contexts_problem(_settings(gitops_deploy_ci_contexts=["other"]))


def test_the_settings_write_hides_the_token():
    task = task_named(BUILDER_TASKS, "Write the builder's configuration")
    assert task["no_log"] is True
    assert task["ansible.builtin.template"]["mode"] == "0640"


def test_the_nfpm_pin_is_ci_s():
    # fact: ansible/roles/setup/homelab_builder/CLAUDE.md#What the builder half writes (H3.5a)
    ci = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    env = ci["jobs"]["host_packages"]["env"]
    assert DEFAULTS["homelab_builder_nfpm_version"] == env["NFPM_VERSION"]
    assert DEFAULTS["homelab_builder_nfpm_sha256"] == env["NFPM_SHA256"]


def test_the_python_install_and_the_unit_name_the_same_uv_directories():
    task = task_named(BUILDER_TASKS, "Install the pinned host Python for the builder")
    unit = directives(render("homelab-builder.service.j2"))
    env = dict(v.split("=", 1) for v in unit["Environment"])
    for name, var in (
        ("UV_CACHE_DIR", "homelab_builder_uv_cache_dir"),
        ("UV_PYTHON_INSTALL_DIR", "homelab_builder_python_dir"),
    ):
        assert task["environment"][name] == "{{ " + var + " }}"
        assert env[name] == DEFAULTS[var].replace(
            "{{ homelab_builder_state_dir }}", DEFAULTS["homelab_builder_state_dir"]
        )


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
