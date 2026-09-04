"""The cron rules `scripts/validate/shell_templates.py` composes, over the real roles tree.

Split out of `test_validate_shell_templates.py`, which keeps the validator machinery. Three
rules and one resolver live here: `lib.cron_targets.cron_job_scripts` (which templates a cron
actually installs), `lib.cron_checks.cron_path_error` (cron inherits no PATH) and
`lib.cron_checks.cron_kubeconfig_error` (cron inherits no KUBECONFIG, and k3s.yaml is
root-only), plus the crowdsec home-allowlist curl-retry pins.

The module-scoped `cron_map` fixture is why these four guards stay in ONE file: pytest runs
with `--dist loadscope`, so a module-scoped fixture is re-evaluated once per module, and
splitting its consumers would pay the ~0.6s tree walk again per file.

Run: uv run pytest scripts/validate/tests/test_shell_template_cron_rules.py
"""

import re
from pathlib import Path

import pytest
from validate import shell_templates as v
from lib import cron_checks as cc
from lib import cron_targets as ct
from lib import shell_lint as sl
from lib.render_guard import ALL_VARS, BASE_CONTEXT, load_yaml


@pytest.fixture(scope="module")
def cron_map():
    """Every cron-installed shell template in the real tree, resolved once.

    The resolver walks every role's tasks and crons to map template -> installing task file,
    which costs ~0.6s. Four guards below read the same mapping and none mutates it, so they
    share one. A test that needs a different roles dir calls ct.cron_job_scripts(roots) itself.
    """
    return ct.cron_job_scripts()


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
    assert template in ct.cron_job_scripts(tmp_path)


def test_cron_job_scripts_excludes_an_archived_role(tmp_path):
    # The rejecting half. The real archive/ tree schedules nothing, so the repo-wide guard
    # below can only ever be observed passing — this is the input it must refuse.
    template = _fixture_role(tmp_path, "containers/archive/retired-role")
    assert template not in ct.cron_job_scripts(tmp_path)
    assert ct.cron_job_scripts(tmp_path) == {}


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
    rendered = sl.render_template(
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
    err = cc.cron_path_error(template, rendered, {template: task_file})
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
    assert cc.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_passes_an_absolute_invocation(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = (
        '#!/bin/bash\nKUBECTL="/usr/local/bin/k3s kubectl -n foo"\n$KUBECTL get pods\n'
    )
    assert cc.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_does_not_flag_kubectl_echoed_in_a_message(tmp_path):
    # registry-gc.sh.j2's real shape: a human-facing suggestion string containing "kubectl",
    # with no bare `k3s` invocation anywhere — must not be flagged as an invocation.
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = (
        '#!/bin/bash\nMSG="job stuck — see: kubectl -n ns logs job/x"\necho "$MSG"\n'
    )
    assert cc.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_ignores_a_bare_invocation_inside_a_comment(tmp_path):
    template = tmp_path / "good.sh.j2"
    task_file = tmp_path / "main.yml"
    task_file.write_text("- name: noop\n")
    rendered = "#!/bin/bash\n# see k3s kubectl for details\necho hi\n"
    assert cc.cron_path_error(template, rendered, {template: task_file}) is None


def test_cron_path_error_is_a_noop_for_a_non_cron_template(tmp_path):
    template = tmp_path / "whatever.sh.j2"
    rendered = 'KUBECTL="k3s kubectl -n foo"\n$KUBECTL get pods\n'
    assert cc.cron_path_error(template, rendered, {}) is None


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
    assert cc.cron_path_error(template, rendered, {template: task_file}) is None


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
        if cc.cron_path_error(
            template, sl.render_template(template, ctx), {template: task_file}
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
    err = cc.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path)
    assert err is not None
    assert "EMPTY cluster" in err


def test_kubeconfig_passes_a_root_cron(tmp_path):
    # k3s.yaml is 0640 root:root, so root needs no KUBECONFIG. Measured on daniel-box 2026-08-27.
    tpl = _cron_role(tmp_path, user="root")
    assert cc.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_treats_a_missing_user_as_nonroot(tmp_path):
    # ansible.builtin.cron defaults `user` to the connection user, which is not root here.
    # Failing closed on the omission is the whole point.
    tpl = _cron_role(tmp_path, user="")
    assert cc.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is not None


def test_kubeconfig_passes_an_export_in_the_script(tmp_path):
    tpl = _cron_role(tmp_path, user="ubuntu")
    rendered = "#!/bin/bash\nexport KUBECONFIG=/home/ubuntu/.kube/config\n" + K3S_BARE
    assert cc.cron_kubeconfig_error(tpl, rendered, roles=tmp_path) is None


def test_kubeconfig_passes_when_the_job_line_sets_it(tmp_path):
    tpl = _cron_role(
        tmp_path, user="ubuntu", job_env="KUBECONFIG=/home/u/.kube/config "
    )
    assert cc.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_flags_an_absolute_invocation_too(tmp_path):
    # THE difference from cron_path_error: an absolute path excuses a PATH problem and does
    # nothing at all for a missing kubeconfig. Two of the three KUBECONFIG-less wrappers in the
    # real tree call /usr/local/bin/k3s, so reusing the PATH rule's selector would have made
    # this rule blind to the shape it is most likely to meet.
    tpl = _cron_role(tmp_path, user="ubuntu")
    assert cc.cron_kubeconfig_error(tpl, K3S_ABS, roles=tmp_path) is not None


def test_kubeconfig_ignores_a_template_that_is_not_a_cron_target(tmp_path):
    (tmp_path / "g" / "r" / "templates").mkdir(parents=True)
    orphan = tmp_path / "g" / "r" / "templates" / "orphan.sh.j2"
    assert cc.cron_kubeconfig_error(orphan, K3S_BARE, roles=tmp_path) is None


def test_kubeconfig_ignores_a_script_that_never_touches_the_cluster(tmp_path):
    tpl = _cron_role(tmp_path, user="ubuntu")
    assert cc.cron_kubeconfig_error(tpl, "#!/bin/bash\ndf -h\n", roles=tmp_path) is None


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
        cc.cron_kubeconfig_error(templates / "rooted.sh.j2", K3S_BARE, roles=tmp_path)
        is None
    )
    assert (
        cc.cron_kubeconfig_error(templates / "user.sh.j2", K3S_BARE, roles=tmp_path)
        is not None
    )


def test_cron_targets_resolve_a_looped_template_task(tmp_path):
    # The looped src/dest form was invisible until 2026-08-27, which hid every longhorn wrapper.
    tpl = _cron_role(tmp_path, user="ubuntu", loop=True)
    assert tpl in ct.cron_job_scripts(tmp_path)


def test_cron_targets_resolve_a_job_line_carrying_jinja_with_spaces(tmp_path):
    # `KUBECONFIG=/home/{{ sys_user }}/.kube/config` has spaces inside the expression, which
    # stopped the job line matching at all — the hole docs-refresh.sh sat in.
    tpl = _cron_role(
        tmp_path,
        user="ubuntu",
        job_env="PATH=/usr/local/bin:/usr/bin:/bin KUBECONFIG=/home/{{ sys_user }}/.kube/config ",
    )
    assert tpl in ct.cron_job_scripts(tmp_path)
    assert cc.cron_kubeconfig_error(tpl, K3S_BARE, roles=tmp_path) is None


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
    for tpl, _task_file, _cron, _env in ct.iter_cron_targets():
        if not tpl.exists():
            continue
        err = cc.cron_kubeconfig_error(tpl, tpl.read_text())
        if err:
            offenders.append(tpl.name)
    assert offenders == [], offenders
