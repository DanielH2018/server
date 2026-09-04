"""Tests for scripts/validate/shell_templates.py — the render-then-lint guard for Jinja-templated
shell scripts (*.sh.j2) that the prek bash-syntax-check / shellcheck hooks can't see (identify
tags a `.sh.j2` as {jinja, text}, never `shell`).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from validate import shell_templates as v
from lib.render_guard import ALL_VARS, BASE_CONTEXT, load_yaml

BACKUP_HEALTH = v.ROLES / "setup" / "k3s" / "templates" / "longhorn-backup-health.sh.j2"
BACKUP_HEALTH_READER = (
    v.ANSIBLE / "roles" / "setup" / "k3s" / "files" / "longhorn_backup_health.py"
)
KUMA_PUSH_LIB = (
    v.ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "kuma-push-lib.sh"
)


@pytest.mark.parametrize(
    ("b2_armed", "r2_armed", "expect_backup_armed", "expect_r2_armed"),
    [
        (True, True, "True", "True"),
        (True, False, "True", "False"),
        (False, True, "False", "True"),
        (False, False, "False", "False"),
    ],
)
def test_backup_health_renders_clean_for_every_arm_state(
    tmp_path,
    b2_armed: bool,
    r2_armed: bool,
    expect_backup_armed: str,
    expect_r2_armed: str,
):
    """Both branches of the armed gates must render to valid shell.

    main() renders with group_vars only, so the ROLE default `k3s_longhorn_backup_armed: false`
    is never applied there and its branch would go unexercised — the dead-path shape that let two
    commands stay broken behind passing tests after the k3s cutover. Since the shim only exports
    LONGHORN_BACKUP_ARMED/LONGHORN_R2_ARMED for the Python reader to interpret (BACKUP_TARGETS
    itself is now derived cluster-side), the disarmed branch that matters is the exported string
    the reader parses — a wrong render there disarms silently instead of at `set -u`.
    """
    shellcheck_bin = shutil.which("shellcheck")
    assert shellcheck_bin, "shellcheck must be on PATH (dev dependency shellcheck-py)"

    ctx = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **v.SHELL_STUB_OVERRIDES,
        "k3s_longhorn_backup_armed": b2_armed,
        "k3s_longhorn_r2_armed": r2_armed,
    }
    rendered = v.render_template(BACKUP_HEALTH, ctx)

    out = tmp_path / "longhorn-backup-health.sh"
    out.write_text(rendered)
    assert v.bash_syntax_check(out) is None
    assert v.shellcheck_check(out, shellcheck_bin) is None

    assert f'export LONGHORN_BACKUP_ARMED="{expect_backup_armed}"' in rendered
    assert f'export LONGHORN_R2_ARMED="{expect_r2_armed}"' in rendered


def test_backup_health_arm_gates_treat_the_string_false_as_disarmed():
    # Ansible's `-e k3s_longhorn_backup_armed=false` passes the STRING "false", which is truthy in
    # Jinja. Without `| bool` an extra-vars disarm would render LONGHORN_BACKUP_ARMED="true" and
    # silently restore the permanently-red monitor this gate exists to prevent.
    ctx = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **v.SHELL_STUB_OVERRIDES,
        "k3s_longhorn_backup_armed": "false",
        "k3s_longhorn_r2_armed": "false",
    }
    rendered = v.render_template(BACKUP_HEALTH, ctx)
    assert 'export LONGHORN_BACKUP_ARMED="False"' in rendered
    assert 'export LONGHORN_R2_ARMED="False"' in rendered


def test_backup_health_logs_unconditionally_even_when_the_reader_itself_breaks():
    """The reader logs its own verdict on a normal run — but on THIS branch it never got that far.

    Without a `logger` call inside the `if ! OUT=$(...)` branch, the one failure mode where the
    local journalctl trail matters most (the Python reader crashing, or `uv` itself missing) is
    the one case that leaves no record at all.
    """
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = v.render_template(BACKUP_HEALTH, ctx)
    reader_failed_branch = rendered.split("if [[ $RC -ne 0", 1)[1].split("else", 1)[0]
    assert "logger -t longhorn-backup-health" in reader_failed_branch


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
        ("1", True),
        ("0", False),
        (1, True),
        (0, False),
        ("", False),
        # Ansible's non-strict boolean() returns False for anything unrecognised rather than
        # raising — the branch that matters, since plain Jinja would call it True.
        ("maybe", False),
    ],
)
def test_ansible_bool_filter_mirrors_ansible_semantics(value, expected: bool):
    assert v._ansible_bool(value) is expected


def test_shellcheck_batch_attributes_findings_to_the_file_that_has_them(tmp_path):
    """One shellcheck process over many files must blame only the dirty one.

    The accept half is the clean file's absence from the result; the reject half is the dirty file's
    presence with its own finding — a batch that flagged everything or nothing would pass a test
    that only checked the return value's truthiness.
    """
    shellcheck_bin = shutil.which("shellcheck")
    assert shellcheck_bin
    clean = tmp_path / "clean.sh"
    clean.write_text('#!/bin/bash\nset -euo pipefail\necho "ok"\n')
    dirty = tmp_path / "dirty.sh"
    dirty.write_text(
        "#!/bin/bash\nfoo=$(ls)\necho $foo\n"
    )  # SC2086: unquoted expansion
    flagged = v.shellcheck_batch([clean, dirty], shellcheck_bin)
    assert clean not in flagged, flagged
    assert dirty in flagged and "SC2086" in flagged[dirty], flagged


def test_shellcheck_batch_of_clean_files_is_empty(tmp_path):
    shellcheck_bin = shutil.which("shellcheck")
    assert shellcheck_bin
    a = tmp_path / "a.sh"
    a.write_text('#!/bin/bash\necho "a"\n')
    assert v.shellcheck_batch([a], shellcheck_bin) == {}
    assert v.shellcheck_batch([], shellcheck_bin) == {}


def test_shellcheck_batch_blames_every_file_when_it_cannot_attribute(tmp_path):
    """A shellcheck that exits non-zero without a parseable finding (a bad flag, a crash) must
    not read as a clean sweep."""
    a = tmp_path / "a.sh"
    a.write_text('#!/bin/bash\necho "a"\n')
    flagged = v.shellcheck_batch([a], "false")  # `false`: exits 1, prints nothing
    assert a in flagged


def test_all_real_shell_templates_render_and_lint_clean():
    # The regression guard: every *.sh.j2 under ansible/roles/ must render with stubbed vars to
    # a script that passes both `bash -n` and shellcheck. Mirrors the sibling validators'
    # real-render tests (validate.compose_templates.test_real_templates_render_clean,
    # validate.config_templates.test_all_real_config_templates_render_to_valid_yaml).
    assert v.main() == 0


def test_discover_templates_finds_the_known_set():
    # Pin the expected set so a template silently going missing (typo'd rename, moved role) is
    # caught, not just "fewer templates checked" sliding by unnoticed.
    names = {p.name for p in v.discover_templates()}
    assert names == {
        "entrypoint.sh.j2",
        "prefs-check.sh.j2",
        "crowdsec-update-home-allowlist.sh.j2",
        "crowdsec-appsec-verify.sh.j2",
        "cloudflare-ip-drift.sh.j2",
        "secret-rotate.sh.j2",
        "secret-rotation-audit.sh.j2",
        "ups-secondary-health.sh.j2",
        "setup-drift-check.sh.j2",
        "docs-refresh.sh.j2",
        "eval-run.sh.j2",
        "pi-sd-health.sh.j2",
        "pi-recovery-health.sh.j2",
        "pull-pi-peers.sh.j2",
        "staging-gate-dispatch.sh.j2",
        "sync-artifacts.sh.j2",
        "portainer-agent-firewall.sh.j2",
        "nut-lan-firewall.sh.j2",
        "prometheus-exporters-lan-firewall.sh.j2",
        "autofix-disk-prune.sh.j2",
        "longhorn-backup-health.sh.j2",
        "longhorn-reap-orphan-backups.sh.j2",
        "longhorn-restore-drill.sh.j2",
        "longhorn-reap-orphan-snapshots.sh.j2",
        "longhorn-trim-volumes.sh.j2",
        "disk-health.sh.j2",
        "remember-logs-health.sh.j2",
        "manifest-prune-check.sh.j2",
        "release-staleness-check.sh.j2",
        "etcd-snapshot-offbox.sh.j2",
        "github-ruleset-drift.sh.j2",
        "telemetry-health.sh.j2",
        "configarr-health.sh.j2",
        "janitorr-health.sh.j2",
        "fake-remux-health.sh.j2",
        "docker-fleet-health.sh.j2",
        "registry-gc.sh.j2",
    }


def test_discover_templates_excludes_vendored_collections():
    # ansible/collections/ ships its own *.sh.j2 test fixtures (community.general) — not ours to
    # lint, same exclusion pytest's testpaths / ruff's extend-exclude already apply.
    assert all("collections" not in p.parts for p in v.discover_templates())


def test_ansible_search_test_mirrors_the_real_jinja_test():
    # No current template uses Ansible's `search` Jinja test (the last one, docker-user-rules.sh.j2,
    # retired at E7 2026-08-13) — vanilla Jinja2 has no `search` test at all (TemplateRuntimeError
    # without this), so this pins the regex-search (not full-match) semantics for whichever
    # template needs it next.
    assert v._ansible_search("172.64.0.0/13", ":") is False
    assert v._ansible_search("2400:cb00::/32", ":") is True
    assert v._ansible_search("ABC", "abc", ignorecase=True) is True
    assert v._ansible_search("ABC", "abc", ignorecase=False) is False


def test_bash_syntax_check_catches_unmatched_quote(tmp_path):
    # The 2026-07-01 kopia bug class (ansible/roles/containers/archive/kopia/files/maintenance-check.sh):
    # an apostrophe broke bash's own quote parsing inside a single-quoted block. Reproduce the
    # shape here — a stray unmatched single quote — and confirm bash -n rejects it.
    broken = tmp_path / "broken.sh"
    broken.write_text("#!/bin/bash\necho 'it's broken'\n")
    err = v.bash_syntax_check(broken)
    assert err is not None
    assert "unexpected" in err or "syntax error" in err


def test_bash_syntax_check_passes_valid_script(tmp_path):
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/bash\nset -euo pipefail\necho hello\n")
    assert v.bash_syntax_check(ok) is None


def test_render_template_stubs_undefined_vars(tmp_path):
    # A var with no BASE_CONTEXT/SHELL_STUB_OVERRIDES/all.yml entry falls back to StubUndefined
    # ("STUB") rather than aborting the render.
    tpl = tmp_path / "sample.sh.j2"
    tpl.write_text("#!/bin/bash\necho {{ some_never_defined_var }}\n")
    rendered = v.render_template(tpl, {})
    assert "STUB" in rendered


def test_check_template_catches_a_broken_render(tmp_path):
    # End-to-end: check_template should surface the unmatched-quote bug via bash -n, the same
    # class of bug that silently killed the kopia maintenance-check watchdog for a day.
    broken_dir = tmp_path / "roles" / "fixture" / "templates"
    broken_dir.mkdir(parents=True)
    broken = broken_dir / "broken.sh.j2"
    broken.write_text("#!/bin/bash\necho '{{ sys_user }}'s broken'\n")

    # check_template resolves the template's path relative to ANSIBLE for reporting, so point
    # ANSIBLE-relative logic at a fixture tree rooted under tmp_path.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig_ansible = v.ANSIBLE
    v.ANSIBLE = tmp_path
    try:
        err = v.check_template(broken, {"sys_user": "ubuntu"}, out_dir, "bash", {})
    finally:
        v.ANSIBLE = orig_ansible

    # "bash" as the shellcheck_bin arg is intentionally wrong (bash isn't shellcheck) — but
    # bash -n runs FIRST and must already catch this broken script before shellcheck is reached.
    assert err is not None
    assert "bash -n" in err


def test_check_template_passes_a_clean_render(tmp_path):
    clean_dir = tmp_path / "roles" / "fixture" / "templates"
    clean_dir.mkdir(parents=True)
    clean = clean_dir / "clean.sh.j2"
    clean.write_text("#!/bin/bash\nset -euo pipefail\necho {{ sys_user }}\n")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig_ansible = v.ANSIBLE
    v.ANSIBLE = tmp_path
    try:
        err = v.check_template(
            clean, {"sys_user": "ubuntu"}, out_dir, v.shutil.which("shellcheck"), {}
        )
    finally:
        v.ANSIBLE = orig_ansible

    assert err is None


def test_main_fails_closed_when_shellcheck_missing(monkeypatch):
    # A missing shellcheck must FAIL the gate, not silently fall back to bash -n alone — the
    # whole point of failing loud instead of degrading (see module docstring / SHELL_STUB_OVERRIDES
    # design comment).
    monkeypatch.setattr(v.shutil, "which", lambda name: None)
    assert v.main() == 1


@pytest.fixture(scope="module")
def cron_map():
    """Every cron-installed shell template in the real tree, resolved once.

    The resolver walks every role's tasks and crons to map template -> installing task file,
    which costs ~0.6s. Four guards below read the same mapping and none mutates it, so they
    share one. A test that needs a different roles dir calls v.cron_job_scripts(roots) itself.
    """
    return v.cron_job_scripts()


def test_cron_job_scripts_resolves_a_dest_rename(cron_map):
    # claude-otel deploys templates/telemetry-health.sh.j2 to
    # /usr/local/bin/claude-otel-health.sh — the cron `job:` only ever names the dest, so this
    # must resolve through the template task's `src:`, not assume dest basename == template name.
    telemetry = v.ROLES / "k8s/claude-otel/templates/telemetry-health.sh.j2"
    assert telemetry in cron_map
    assert cron_map[telemetry].name == "main.yml"


def test_cron_job_scripts_excludes_archive(cron_map):
    # Match relative to the roles root: an absolute-path substring check also matches every
    # path in a checkout that happens to sit under a directory named "archive", which is how
    # this guard failed in a worktree called `archive-slice-prefix-decision`.
    assert not any("archive" in t.relative_to(v.ROLES).parts for t in cron_map)


def _fixture_role(roles: Path, role_dir: str) -> Path:
    """Write a minimal role at `roles/<role_dir>` that schedules one shell template."""
    role = roles / role_dir
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir(parents=True)
    (role / "templates" / "job.sh.j2").write_text("#!/bin/bash\ntrue\n")
    (role / "tasks" / "main.yml").write_text(
        "- name: Install\n"
        "  ansible.builtin.template:\n"
        "    src: job.sh.j2\n"
        "    dest: /usr/local/bin/job.sh\n"
        "- name: Schedule\n"
        "  ansible.builtin.cron:\n"
        "    name: job\n"
        "    job: /usr/local/bin/job.sh\n"
    )
    return role / "templates" / "job.sh.j2"


def test_cron_job_scripts_finds_a_live_role(tmp_path):
    # The accepting half of the pair below: without it, an exclusion that swallowed everything
    # would be indistinguishable from one that excludes only archive/.
    template = _fixture_role(tmp_path, "containers/live-role")
    assert template in v.cron_job_scripts(tmp_path)


def test_cron_job_scripts_excludes_an_archived_role(tmp_path):
    # The rejecting half. The real archive/ tree schedules nothing, so the repo-wide guard
    # below can only ever be observed passing — this is the input it must refuse.
    template = _fixture_role(tmp_path, "containers/archive/retired-role")
    assert template not in v.cron_job_scripts(tmp_path)
    assert v.cron_job_scripts(tmp_path) == {}


def test_cron_job_scripts_excludes_the_deliberately_unscheduled_reaper(cron_map):
    # longhorn-reap-orphan-backups.sh.j2 is an operator-invoked tool — health-crons.yml installs
    # it (release_bin) but deliberately never wires it to a `cron:` task, because the
    # safe-to-delete set depends on live state the script can check but not guarantee will
    # still hold on an unattended schedule. It must not appear in the cron job map.
    assert not any(t.name == "longhorn-reap-orphan-backups.sh.j2" for t in cron_map)


@pytest.mark.parametrize(
    "template_name",
    ["longhorn-reap-orphan-backups.sh.j2", "longhorn-reap-orphan-snapshots.sh.j2"],
)
def test_reap_orphan_shims_export_the_uv_python_install_dir(template_name):
    # `--apply` is documented as `sudo <shim> --apply`, and sudo resets HOME to /root by
    # default. The pinned interpreter is installed `become: false` under sys_user's own HOME
    # (~/.local/share/uv/python), so under sudo's /root HOME uv would find no interpreter and
    # --no-python-downloads forbids fetching one. Exporting UV_PYTHON_INSTALL_DIR before the
    # `uv run` call makes discovery independent of which HOME invoked the shim.
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = v.render_template(
        v.ROLES / "setup" / "k3s" / "templates" / template_name, ctx
    )
    sys_user = ctx["sys_user"]
    assert (
        'export UV_PYTHON_INSTALL_DIR="/home/%s/.local/share/uv/python"' % sys_user
    ) in rendered
    # And the export must land BEFORE the `uv run` invocation it exists to fix.
    export_line = next(
        i for i, ln in enumerate(rendered.splitlines()) if "UV_PYTHON_INSTALL_DIR" in ln
    )
    uv_run_line = next(
        i for i, ln in enumerate(rendered.splitlines()) if "uv run" in ln
    )
    assert export_line < uv_run_line


def test_cron_path_error_flags_a_bare_invocation_with_no_path_fix(tmp_path):
    template = tmp_path / "bad.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = '#!/bin/bash\nKUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
    err = v.cron_path_error(template, rendered, {template: task_file})
    assert err is not None
    assert "cron job target" in err


def test_cron_path_error_passes_a_path_export(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = (
        '#!/bin/bash\nexport PATH="/usr/local/bin:${PATH}"\n'
        'KUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
    )
    assert v.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_passes_an_absolute_invocation(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = (
        '#!/bin/bash\nKUBECTL="/usr/local/bin/k3s kubectl -n foo"\n$KUBECTL get pods\n'
    )
    assert v.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_does_not_flag_kubectl_echoed_in_a_message(tmp_path):
    # registry-gc.sh.j2's real shape: a human-facing suggestion string containing "kubectl",
    # with no bare `k3s` invocation anywhere — must not be flagged as an invocation.
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = (
        '#!/bin/bash\nMSG="job stuck — see: kubectl -n ns logs job/x"\necho "$MSG"\n'
    )
    assert v.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_ignores_a_bare_invocation_inside_a_comment(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = "#!/bin/bash\n# see k3s kubectl for details\necho hi\n"
    assert v.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_is_a_noop_for_a_non_cron_template(tmp_path):
    template = tmp_path / "whatever.sh.j2"
    rendered = 'KUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
    assert v.cron_path_error(template, rendered, {}) is None


def test_cron_path_error_passes_when_the_crontab_sets_path(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text(
        "- name: Schedule the PATH line\n"
        "  ansible.builtin.cron:\n"
        "    env: yes\n"
        "    name: PATH\n"
        "    value: /usr/local/bin:/usr/bin:/bin\n"
    )
    rendered = '#!/bin/bash\nKUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
    assert v.cron_path_error(template, rendered, {template: task_file}) is None


def test_no_cron_job_template_in_the_tree_violates_the_path_rule(cron_map):
    # The rule shipped with two real offenders (telemetry-health, longhorn-backup-health);
    # both were fixed in the same change, so there is no allowlist. Asserting zero against
    # the real tree keeps that true without a list to go stale.
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    assert cron_map, (
        "cron_job_scripts() found no cron-installed templates — resolver broke"
    )
    offenders = {
        str(template.relative_to(v.ROLES))
        for template, task_file in cron_map.items()
        if v.cron_path_error(
            template, v.render_template(template, ctx), {template: task_file}
        )
    }
    assert offenders == set()


HOME_ALLOWLIST = (
    v.ROLES / "k8s" / "crowdsec" / "templates" / "crowdsec-update-home-allowlist.sh.j2"
)

# How WIDE the retry has to be is a separate question with its own budget, and it lives in
# ansible/tests/services/test_crowdsec_allowlist_push_retry.py. This guard only pins that a
# count is there at all, so the two do not both have to move when the width is retuned.
_RETRY_COUNT = re.compile(r"--retry\s+\d+")


def test_home_allowlist_curls_all_retry():
    # The monitor this script pushes is push-type with max_retries 0, so ONE transient curl
    # failure is an immediate DOWN — 9 down/up cycles in the 3 days to 2026-08-24, none of them
    # a real allowlist problem. Both `bash -n` and shellcheck pass a script with the flags
    # stripped, so the DECIDED comment above them is the only thing holding them today, and a
    # comment is not enforcement. Pin every curl instead.
    lines = [
        ln.strip()
        for ln in HOME_ALLOWLIST.read_text().splitlines()
        if "curl " in ln and not ln.lstrip().startswith("#")
    ]
    assert lines, "no curl invocations found — did the script move or get rewritten?"
    for ln in lines:
        assert _RETRY_COUNT.search(ln), f"curl without a --retry count: {ln}"
        # --retry-all-errors is what covers connection-refused, and a rollout's 404. Measured on
        # curl 8.5.0: bare --retry returns instantly on a refused port (exit 7) and on a 404; it
        # DOES retry a DNS failure.
        assert "--retry-all-errors" in ln, f"curl without --retry-all-errors: {ln}"


def test_a_home_allowlist_curl_with_the_retry_flags_stripped_is_flagged():
    stripped = 'curl -fsS --max-time 10 -G -K - --data-urlencode "status=$1"'
    assert not _RETRY_COUNT.search(stripped)
    assert "--retry-all-errors" not in stripped


def test_home_allowlist_fail_logs_the_status_down_prefix():
    # `probe.py alerts` reconstructs host-cron episodes from `{job="syslog"} |= "status=down"`
    # (parse_syslog_down_line). This script logged a bare message until 2026-08-24, so 7 days of
    # flaps left zero rows there and nothing to diagnose from once Kuma's current state cleared.
    fail_line = next(
        ln for ln in HOME_ALLOWLIST.read_text().splitlines() if ln.startswith("fail()")
    )
    assert 'logger -t crowdsec-home-allowlist "status=down $1"' in fail_line, fail_line


# ── cron_kubeconfig_error (cron inherits no KUBECONFIG, and k3s.yaml is root-only) ──


def _cron_role(root, *, script="w.sh.j2", user="ubuntu", job_env="", loop=False):
    """Build a minimal roles tree with one template task and one cron scheduling it.

    Returns the template path cron_kubeconfig_error expects to be handed.
    """
    tasks_dir = root / "g" / "r" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (root / "g" / "r" / "templates").mkdir(parents=True, exist_ok=True)
    dest = "/usr/local/bin/" + script.replace(".j2", "")
    if loop:
        tpl_task = (
            "- name: install\n"
            "  ansible.builtin.template:\n"
            '    src: "{{ item.src }}"\n'
            '    dest: "{{ item.dest }}"\n'
            "  loop:\n"
            f"    - src: {script}\n"
            f"      dest: {dest}\n"
        )
    else:
        tpl_task = (
            "- name: install\n"
            "  ansible.builtin.template:\n"
            f"    src: {script}\n"
            f"    dest: {dest}\n"
        )
    cron_task = (
        "- name: schedule\n"
        "  ansible.builtin.cron:\n"
        '    name: "job"\n'
        + (f"    user: {user}\n" if user else "")
        + f'    job: "{job_env}{dest}"\n'
    )
    (tasks_dir / "main.yml").write_text(tpl_task + cron_task)
    return root / "g" / "r" / "templates" / script


K3S_BARE = '#!/bin/bash\nKUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
K3S_ABS = "#!/bin/bash\n/usr/local/bin/k3s kubectl -n foo get pods\n"


def test_kubeconfig_flags_a_nonroot_cron_touching_the_cluster(tmp_path):
    tpl = _cron_role(tmp_path, user="ubuntu")
    err = v.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path)
    assert err is not None
    assert "EMPTY cluster" in err


def test_kubeconfig_passes_a_root_cron(tmp_path):
    # k3s.yaml is 0640 root:root, so root needs no KUBECONFIG. Measured on daniel-box 2026-08-27.
    tpl = _cron_role(tmp_path, user="root")
    assert v.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_treats_a_missing_user_as_nonroot(tmp_path):
    # ansible.builtin.cron defaults `user` to the connection user, which is not root here.
    # Failing closed on the omission is the whole point.
    tpl = _cron_role(tmp_path, user="")
    assert v.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is not None


def test_kubeconfig_passes_an_export_in_the_script(tmp_path):
    tpl = _cron_role(tmp_path, user="ubuntu")
    rendered = "#!/bin/bash\nexport KUBECONFIG=/home/ubuntu/.kube/config\n" + K3S_BARE
    assert v.cron_kubeconfig_error(tpl, rendered, roles=tmp_path) is None


def test_kubeconfig_passes_when_the_job_line_sets_it(tmp_path):
    tpl = _cron_role(
        tmp_path, user="ubuntu", job_env="KUBECONFIG=/home/u/.kube/config "
    )
    assert v.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_flags_an_absolute_invocation_too(tmp_path):
    # THE difference from cron_path_error: an absolute path excuses a PATH problem and does
    # nothing at all for a missing kubeconfig. Two of the three KUBECONFIG-less wrappers in the
    # real tree call /usr/local/bin/k3s, so reusing the PATH rule's selector would have made
    # this rule blind to the shape it is most likely to meet.
    tpl = _cron_role(tmp_path, user="ubuntu")
    assert v.cron_kubeconfig_error(tpl, K3S_ABS, roles=tmp_path) is not None


def test_kubeconfig_ignores_a_template_that_is_not_a_cron_target(tmp_path):
    (tmp_path / "g" / "r" / "templates").mkdir(parents=True)
    orphan = tmp_path / "g" / "r" / "templates" / "orphan.sh.j2"
    assert v.cron_kubeconfig_error(orphan, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_ignores_a_script_that_never_touches_the_cluster(tmp_path):
    tpl = _cron_role(tmp_path, user="ubuntu")
    assert v.cron_kubeconfig_error(tpl, "#!/bin/bash\ndf -h\n", roles=tmp_path) is None


def test_kubeconfig_reads_the_user_of_the_matching_task_not_the_file(tmp_path):
    # The failure mode this rule is most likely to have: health-crons.yml schedules eight crons
    # with different users, so a file-wide search for `user: root` would let one task excuse a
    # sibling. Both templates live in one task file; only the non-root one may be flagged.
    tasks_dir = tmp_path / "g" / "r" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tmp_path / "g" / "r" / "templates").mkdir(parents=True)
    tasks_dir.joinpath("main.yml").write_text(
        "- name: install both\n"
        "  ansible.builtin.template:\n"
        '    src: "{{ item.src }}"\n'
        '    dest: "{{ item.dest }}"\n'
        "  loop:\n"
        "    - src: rooted.sh.j2\n"
        "      dest: /usr/local/bin/rooted.sh\n"
        "    - src: user.sh.j2\n"
        "      dest: /usr/local/bin/user.sh\n"
        "- name: schedule rooted\n"
        "  ansible.builtin.cron:\n"
        '    name: "rooted"\n'
        "    user: root\n"
        '    job: "/usr/local/bin/rooted.sh"\n'
        "- name: schedule user\n"
        "  ansible.builtin.cron:\n"
        '    name: "user"\n'
        "    user: ubuntu\n"
        '    job: "/usr/local/bin/user.sh"\n'
    )
    templates = tmp_path / "g" / "r" / "templates"
    assert (
        v.cron_kubeconfig_error(templates / "rooted.sh.j2", K3S_BARE, roles=tmp_path)
        is None
    )
    assert (
        v.cron_kubeconfig_error(templates / "user.sh.j2", K3S_BARE, roles=tmp_path)
        is not None
    )


def test_cron_targets_resolve_a_looped_template_task(tmp_path):
    # The looped src/dest form was invisible until 2026-08-27, which hid every longhorn wrapper.
    tpl = _cron_role(tmp_path, user="ubuntu", loop=True)
    assert tpl in v.cron_job_scripts(tmp_path)


def test_cron_targets_resolve_a_job_line_carrying_jinja_with_spaces(tmp_path):
    # `KUBECONFIG=/home/{{ sys_user }}/.kube/config` has spaces inside the expression, which
    # stopped the job line matching at all — the hole docs-refresh.sh sat in.
    tpl = _cron_role(
        tmp_path,
        user="ubuntu",
        job_env="PATH=/usr/local/bin:/usr/bin:/bin KUBECONFIG=/home/{{ sys_user }}/.kube/config ",
    )
    assert tpl in v.cron_job_scripts(tmp_path)
    assert v.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


def test_the_widened_resolver_covers_the_scripts_it_was_written_for(cron_map):
    # Pin the four the parser fixes brought in, so a later narrowing of either fix shows up
    # here rather than as a guard quietly covering less than it claims.
    names = {t.name for t in cron_map}
    for expected in (
        "longhorn-backup-health.sh.j2",
        "longhorn-restore-drill.sh.j2",
        "longhorn-trim-volumes.sh.j2",
        "docs-refresh.sh.j2",
    ):
        assert expected in names, expected


def test_the_real_tree_has_no_kubeconfig_violation():
    # Zero today, and this is the assertion that says so out loud: the rule ships preventive,
    # not as a fix. A future non-root cron wrapper that reads the cluster fails here first.
    offenders = []
    for tpl, _task_file, _cron, _env in v._cron_targets():
        if not tpl.exists():
            continue
        err = v.cron_kubeconfig_error(tpl, tpl.read_text())
        if err:
            offenders.append(tpl.name)
    assert offenders == [], offenders


def test_backup_health_shim_exports_every_env_var_the_reader_requires():
    """LONGHORN_* names are derived from the reader's OWN source, not hardcoded here.

    Every LONGHORN_* var the reader reads is REQUIRED — `_require_env`/`_require_int_env`/
    `_require_bool_env`, no hardcoded fallback (the 2026-09-04 review's finding #3: a fallback
    used to let a shim that stopped exporting one var substitute a stale constant silently). The
    two sides must therefore agree exactly: this derives the required set straight from
    longhorn_backup_health.py's source so a var added to one side without the other is caught
    here, rather than by the reader exiting nonzero in production naming the var nobody remembered
    to export.
    """
    reader_source = BACKUP_HEALTH_READER.read_text()
    required = set(
        re.findall(r'_require_\w*env\("(LONGHORN_[A-Z0-9_]+)"\)', reader_source)
    )
    assert len(required) >= 13, (
        f"the derivation found suspiciously few required vars: {required} — "
        "did _require_env's call shape change?"
    )

    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = v.render_template(BACKUP_HEALTH, ctx)
    exported = set(
        re.findall(r"^export (LONGHORN_[A-Z0-9_]+)=", rendered, re.MULTILINE)
    )

    missing = required - exported
    assert not missing, f"the shim does not export: {sorted(missing)}"


FAKE_HC_PING_KEY = "fixture-hc-ping-key"


def _run_rendered_shim(
    tmp_path,
    fake_reader_body: str,
    *,
    push_ok_unset: bool = False,
    extra_stub_files: dict[str, str] | None = None,
):
    """Run the real rendered backup-health shim with every external seam pointed at a fixture.

    The seams: the reader invocation becomes `fake_reader_body`; `kuma_push` and
    `boot_grace_active` are shadowed by shell functions; the Kuma token file and the
    healthchecks.io key file are swapped for tmp copies; and `curl` is a stub on PATH that
    records its argv. Returns `(proc, kuma_status, curl_calls)` where `kuma_status` is the
    string the `kuma_push` stub received (None when never called) and `curl_calls` is one
    argv line per curl invocation.

    `push_ok_unset` reproduces an older kuma-push-lib.sh that never set KUMA_PUSH_OK at all
    (2026-09-04 review finding #4) — the injected `kuma_push` stub omits that assignment.
    `extra_stub_files` places additional executables (name -> script body) on the same PATH
    directory as the `curl` stub, ahead of the real binaries — e.g. a `mktemp` that fails.

    The healthchecks.io seam is not optional on this host: `/etc/healthchecks/ping.env` is
    0640 root:ubuntu, so the test user CAN read the real key, and until this seam existed a
    test run sourced it and sent a fixture `up` ping to the real off-site dead-man for both
    `longhorn-backup-health` and `uptime-kuma-alive`. Two independent guards: the path is
    replaced, and `curl` is stubbed so a missed replacement leaks a real key into a file
    under tmp_path rather than onto the wire. The assertions on `curl_calls` never print a
    URL for that reason.
    """
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = v.render_template(BACKUP_HEALTH, ctx)

    fake_reader = tmp_path / "fake-reader.sh"
    fake_reader.write_text("#!/usr/bin/env bash\n" + fake_reader_body)
    fake_reader.chmod(0o755)

    push_token_env = tmp_path / "kuma-push.env"
    push_token_env.write_text("LONGHORN_BACKUP_PUSH_TOKEN='test-token'\n")
    hc_ping_env = tmp_path / "ping.env"
    hc_ping_env.write_text(f"HC_PING_KEY='{FAKE_HC_PING_KEY}'\n")
    kuma_push_call = tmp_path / "kuma-push-call.txt"

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl_calls = stub_bin / "curl-calls"
    curl_calls.touch()
    curl = stub_bin / "curl"
    curl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {curl_calls}\n")
    curl.chmod(0o755)
    for name, body in (extra_stub_files or {}).items():
        stub = stub_bin / name
        stub.write_text(body)
        stub.chmod(0o755)

    script = rendered
    script = script.replace(
        "source /usr/local/lib/kuma-push-lib.sh ||", f"source {KUMA_PUSH_LIB} ||"
    )
    script = script.replace("/etc/rancher/k3s/kuma-push.env", str(push_token_env))
    script = script.replace("/etc/healthchecks/ping.env", str(hc_ping_env))
    reader_invocation = re.search(
        r"/usr/local/bin/uv run --no-project --no-python-downloads --python \S+ "
        r"/opt/longhorn-backup-health/longhorn_backup_health\.py",
        script,
    )
    assert reader_invocation, "the reader invocation line moved; update this test"
    script = script.replace(reader_invocation.group(0), str(fake_reader))
    # boot_grace_active/kuma_push are shell FUNCTIONS bash resolves at call time, so defining our
    # own here — after the real `source` above, before either is actually called further down —
    # shadows the sourced versions for the rest of this run without needing to fake the library.
    push_ok_assignment = "" if push_ok_unset else "KUMA_PUSH_OK=1;"
    script = script.replace(
        "if boot_grace_active ",
        f"boot_grace_active() {{ return 1; }}\n"
        # KUMA_PUSH_OK is set by the real kuma_push and read further down by the healthchecks
        # block (`(( ${{KUMA_PUSH_OK:-1}} ))` under `set -u`) — `push_ok_unset` reproduces an
        # older lib that never made that assignment at all.
        f'kuma_push() {{ printf "%s" "$1" > {kuma_push_call}; {push_ok_assignment} }}\n'
        "if boot_grace_active ",
        1,
    )

    script_path = tmp_path / "longhorn-backup-health.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    kuma_status = kuma_push_call.read_text() if kuma_push_call.exists() else None
    return proc, kuma_status, curl_calls.read_text().splitlines()


def _assert_pings_carry_only_the_fixture_key(curl_calls: list[str]) -> None:
    # Deliberately no URL in the failure message: a missed ping.env replacement means these
    # lines hold the REAL healthchecks key.
    assert len(curl_calls) == 2, f"expected both hc-ping calls, got {len(curl_calls)}"
    assert all(FAKE_HC_PING_KEY in call for call in curl_calls), (
        "a healthchecks ping did not carry the fixture key: the ping.env path replacement "
        "in _run_rendered_shim missed, and the real key was sourced"
    )


def test_backup_health_kubectl_stderr_does_not_contaminate_the_status(
    tmp_path, logger_calls
):
    """Regression for the 2026-09-04 review's finding #1, run against the ACTUAL shipped shim.

    Until this fix, `OUT=$(... 2>&1)` meant any stderr byte the reader's own `logger` subprocess
    wrote — `logger: socket /dev/log: ...`, e.g. — landed ahead of the reader's real stdout line
    once the two streams were merged, silently turning `up` into garbage that Kuma reads as DOWN
    and pings healthchecks.io `/fail` on a backup plane that was fine. This patches in a fake
    reader that writes junk to stderr before printing `up<TAB>ok` and asserts on the status the
    `kuma_push` stub actually receives — proving the fix at the point that matters (what reaches
    Kuma) rather than just that the fix's source text exists.

    On this path the shim now DOES call `logger` — the 2026-09-04 review's finding #2. Only the
    failure branch used to call it, so this exact stderr (a green run with something on stderr)
    used to be silently dropped; see test_backup_health_logs_stderr_on_a_successful_run right
    below for that half in isolation.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'logger: socket /dev/log: No such file or directory\\n' >&2\n"
        "printf 'up\\tbackup target(s) default available, 1 backed-up volume(s) covered\\n'\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status is not None, "kuma_push was never called"
    assert kuma_status == "up", (
        f"stderr contamination reached STATUS: got {kuma_status!r}, "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    lines = logger_calls.read_text().splitlines()
    assert any("No such file or directory" in line for line in lines), lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


def test_backup_health_logs_stderr_on_a_successful_run(tmp_path, logger_calls):
    """Delta 2 (2026-09-04 review): a clean run's stderr must reach the local trail too.

    Before this fix only the `if [[ $RC -ne 0 ...` branch called `logger` — a kubectl RBAC
    warning or a uv resolution warning on an otherwise-green tick was captured into ERR and then
    silently discarded, since the success branch never read it. journalctl showed nothing for
    the one case where "the run succeeded, but something on stderr is worth knowing" is exactly
    the signal a warning exists to carry.
    """
    proc, kuma_status, _curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'uv: warning: pin resolution took a fallback path\\n' >&2\n"
        "printf 'up\\tall clean\\n'\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    lines = logger_calls.read_text().splitlines()
    assert any("pin resolution took a fallback path" in line for line in lines), lines


def test_backup_health_logs_nothing_on_a_clean_run_with_empty_stderr(
    tmp_path, logger_calls
):
    """The clean half of the pair above: no stderr, no logger call — the guard must not fire on
    nothing, which would otherwise mask the one case (an actual failure) it exists to explain.
    """
    proc, kuma_status, _curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    assert logger_calls.read_text() == ""


def test_backup_health_reader_failure_is_logged_through_the_stub(
    tmp_path, logger_calls
):
    """A reader that exits nonzero is pushed DOWN and logged, and the log hits the stub.

    The end-to-end half of `test_backup_health_logs_unconditionally_even_when_the_reader_itself_
    breaks`, which only asserts the `logger` call's source text sits in the right branch. It is
    also the non-vacuity proof for this directory's `_no_syslog` fixture: the shim's `logger`
    is the one call on any tested path, so an empty `logger_calls` here would mean the stub is
    no longer first on PATH and the real syslog took it (issue #1052).
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'Traceback: the reader broke\\n' >&2\nexit 1\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert lines == [
        "-t longhorn-backup-health status=down longhorn backup health reader failed: "
        "Traceback: the reader broke"
    ], lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


# ── delta 1 (2026-09-04 review): STATUS must be Kuma's own "up"/"down" vocabulary ───────────


def test_backup_health_a_recognized_down_status_reaches_kuma_unchanged(
    tmp_path, logger_calls
):
    """The clean half: a real DOWN from the reader must reach Kuma verbatim."""
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'down\\tbackup target unavailable\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    _assert_pings_carry_only_the_fixture_key(curl_calls)


def test_backup_health_a_tabless_last_line_is_flagged_not_pushed_as_is(
    tmp_path, logger_calls
):
    """The flagged half. A stray final stdout line with no tab used to make STATUS the whole
    line — neither "up" nor "down" — and get pushed to Kuma as-is; `[[ "$STATUS" == "up" ]]`
    then read false and pinged healthchecks.io `/fail` on a status string Kuma never defined.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'a stray line with no tab at all\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert any("unrecognized status" in line for line in lines), lines
    # And it must page — the whole point of catching this rather than pushing it as-is.
    assert any("/fail" in call for call in curl_calls), curl_calls


# ── delta 3 (2026-09-04 review): an unchecked mktemp must not go unexplained ────────────────


def test_backup_health_a_failing_mktemp_is_named_in_the_pushed_message(
    tmp_path, logger_calls
):
    """A full or read-only /tmp must be reported as ITSELF, not as an opaque reader failure with
    no clue why. `mktemp` is stubbed to fail — the same PATH seam `curl` already uses.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'up\\tall clean\\n'\n",
        extra_stub_files={"mktemp": "#!/bin/sh\nexit 1\n"},
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert any("mktemp" in line for line in lines), lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


# ── delta 4 (2026-09-04 review): KUMA_PUSH_OK must not be a bare reference under `set -u` ───


def test_backup_health_kuma_alive_ping_has_no_fail_suffix_when_the_push_succeeded(
    tmp_path, logger_calls
):
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    alive_calls = [c for c in curl_calls if "uptime-kuma-alive" in c]
    assert len(alive_calls) == 1
    assert "/fail" not in alive_calls[0]


def test_backup_health_an_older_lib_that_never_sets_kuma_push_ok_does_not_abort(
    tmp_path, logger_calls
):
    """The regression this delta exists for: under `set -u`, a bare `(( KUMA_PUSH_OK ))`
    reference is fatal the instant kuma-push-lib.sh doesn't set it — an older lib on the host,
    predating this var. That used to kill the script before the hc-ping call even ran, silencing
    the off-site deadman rather than reddening it. `${KUMA_PUSH_OK:-1}` must let the script keep
    running (with the default-success reading, matching pi-sd-health.sh.j2's own
    `${KUMA_PUSH_OK:-1}` convention) and still reach both curl calls.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n", push_ok_unset=True
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    assert "unbound variable" not in proc.stderr
    assert any("longhorn-backup-health" in c for c in curl_calls)
    assert any("uptime-kuma-alive" in c for c in curl_calls)
