"""Tests for scripts/validate/shell_templates.py — the render-then-lint guard for Jinja-templated
shell scripts (*.sh.j2) that the prek bash-syntax-check / shellcheck hooks can't see (identify
tags a `.sh.j2` as {jinja, text}, never `shell`).
"""

import shutil

import pytest
from validate import shell_templates as v
from lib.render_guard import ALL_VARS, BASE_CONTEXT, load_yaml

BACKUP_HEALTH = v.ROLES / "setup" / "k3s" / "templates" / "longhorn-backup-health.sh.j2"


@pytest.mark.parametrize(
    ("b2_armed", "r2_armed", "expect_armed", "expect_disarmed"),
    [
        (True, True, ["default", "r2"], []),
        (True, False, ["default"], ["r2"]),
        (False, True, ["r2"], ["default"]),
        (False, False, [], ["default", "r2"]),
    ],
)
def test_backup_health_renders_clean_for_every_arm_state(
    tmp_path, b2_armed: bool, r2_armed: bool, expect_armed, expect_disarmed
):
    """Both branches of the armed gates must render to valid shell.

    main() renders with group_vars only, so the ROLE default `k3s_longhorn_backup_armed: false`
    is never applied there and its branch would go unexercised — the dead-path shape that let two
    commands stay broken behind passing tests after the k3s cutover. The disarmed branch is the
    one that matters most: it is what runs whenever B2 is off, and an empty BACKUP_TARGETS array
    under `set -u` is exactly the kind of thing that only fails at 03:30.
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

    def targets(prefix: str) -> list[str]:
        line = next(ln for ln in rendered.splitlines() if ln.startswith(prefix))
        return line[len(prefix) : -1].split()

    assert targets("BACKUP_TARGETS=(") == expect_armed
    assert targets("DISARMED_TARGETS=(") == expect_disarmed


def test_backup_health_arm_gates_treat_the_string_false_as_disarmed():
    # Ansible's `-e k3s_longhorn_backup_armed=false` passes the STRING "false", which is truthy in
    # Jinja. Without `| bool` an extra-vars disarm would leave `default` in the armed set and
    # silently restore the permanently-red monitor this gate exists to prevent.
    ctx = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **v.SHELL_STUB_OVERRIDES,
        "k3s_longhorn_backup_armed": "false",
        "k3s_longhorn_r2_armed": "false",
    }
    rendered = v.render_template(BACKUP_HEALTH, ctx)
    assert "BACKUP_TARGETS=()" in rendered
    assert "DISARMED_TARGETS=(default r2)" in rendered


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


def test_cron_job_scripts_excludes_the_deliberately_unscheduled_reaper(cron_map):
    # longhorn-reap-orphan-backups.sh.j2 uses the same bare `k3s kubectl` as the two real
    # offenders but health-crons.yml deliberately never schedules it — it must not appear here.
    assert not any(t.name == "longhorn-reap-orphan-backups.sh.j2" for t in cron_map)


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
        assert "--retry 3" in ln, f"curl without --retry 3: {ln}"
        # --retry-all-errors is what covers connection-refused. Measured on curl 8.5.0, bare
        # --retry returns instantly on a refused port (exit 7); it DOES retry a DNS failure.
        assert "--retry-all-errors" in ln, f"curl without --retry-all-errors: {ln}"


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
