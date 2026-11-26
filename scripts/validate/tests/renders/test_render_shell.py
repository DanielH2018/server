"""Tests for scripts/validate/render_lib/shell.py and the scripts/lib/ modules it composes.

This module keeps the validator machinery: template discovery, the render/lint/syntax passes,
the shellcheck batch attribution and `run`'s fail-closed behaviour. The rendered
longhorn-backup-health shim is tested in `test_backup_health_shim.py`.

Run: uv run pytest scripts/validate/tests/renders/test_render_shell.py
"""

import shutil

from lib.estate import host_vars
from validate.render_lib import shell as v
from validate.validate_lib import shell_lint as sl
from validate.validate_lib.shipped_render import render_shipped_text


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
    flagged = sl.shellcheck_batch([clean, dirty], shellcheck_bin)
    assert clean not in flagged, flagged
    assert dirty in flagged and "SC2086" in flagged[dirty], flagged


def test_shellcheck_batch_of_clean_files_is_empty(tmp_path):
    shellcheck_bin = shutil.which("shellcheck")
    assert shellcheck_bin
    a = tmp_path / "a.sh"
    a.write_text('#!/bin/bash\necho "a"\n')
    assert sl.shellcheck_batch([a], shellcheck_bin) == {}
    assert sl.shellcheck_batch([], shellcheck_bin) == {}


def test_shellcheck_batch_blames_every_file_when_it_cannot_attribute(tmp_path):
    """A shellcheck that exits non-zero without a parseable finding (a bad flag, a crash) must
    not read as a clean sweep."""
    a = tmp_path / "a.sh"
    a.write_text('#!/bin/bash\necho "a"\n')
    flagged = sl.shellcheck_batch([a], "false")  # `false`: exits 1, prints nothing
    assert a in flagged


def test_discover_templates_finds_the_known_set():
    # Pin the expected set so a template silently going missing is caught.
    names = {p.name for p in v.discover_templates()}
    assert names == {
        "claude-agent-browser.sh.j2",
        "renovate-agent-setup.sh.j2",
        "claude-triage-setup.sh.j2",
        "claude-agent-home.sh.j2",
        "secret-rotation-audit.sh.j2",
        "ups-secondary-health.sh.j2",
        "aide-check.sh.j2",
        "rkhunter-check.sh.j2",
        "docs-refresh.sh.j2",
        "eval-run.sh.j2",
        "worktree-sweep.sh.j2",
        "board-sync.sh.j2",
        "pi-sd-health.sh.j2",
        "pi-gz-integrity.sh.j2",
        "pi-recovery-health.sh.j2",
        "pi-watchdog-health.sh.j2",
        "pi-audit-health.sh.j2",
        "etcd-restore-drill-vm.sh.j2",
        "etcd-drill-prepare.sh.j2",
        "sync-artifacts.sh.j2",
        "longhorn-backup-health.sh.j2",
        "longhorn-restore-drill.sh.j2",
        "disk-health.sh.j2",
        "loki-read-route-health.sh.j2",
        "etcd-token-drift.sh.j2",
        "github-ruleset-drift.sh.j2",
        "github-interaction-limit.sh.j2",
        "claude-otel-health.sh.j2",
        "claude-guardrail-audit.sh.j2",
        "fake-remux-health.sh.j2",
        "registry-gc.sh.j2",
        "claude-agent-setup.sh.j2",
    }


def test_bash_syntax_check_catches_unmatched_quote(tmp_path):
    broken = tmp_path / "broken.sh"
    broken.write_text("#!/bin/bash\necho 'it's broken'\n")
    err = sl.bash_syntax_check(broken)
    assert err is not None
    assert "unexpected" in err or "syntax error" in err


def test_bash_syntax_check_passes_valid_script(tmp_path):
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/bash\nset -euo pipefail\necho hello\n")
    assert sl.bash_syntax_check(ok) is None


def test_render_template_stubs_undefined_vars(tmp_path):
    # A var with no entry falls back to StubUndefined ("STUB") rather than aborting the render.
    tpl = tmp_path / "sample.sh.j2"
    tpl.write_text("#!/bin/bash\necho {{ some_never_defined_var }}\n")
    rendered = render_shipped_text(tpl, {})
    assert "STUB" in rendered


def test_check_template_catches_a_broken_render(tmp_path):
    broken_dir = tmp_path / "roles" / "fixture" / "templates"
    broken_dir.mkdir(parents=True)
    broken = broken_dir / "broken.sh.j2"
    broken.write_text("#!/bin/bash\necho '{{ sys_user }}'s broken'\n")

    # check_template resolves the template's path relative to REPO, so point it at a fixture tree
    # under tmp_path.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig_repo = v.REPO
    v.REPO = tmp_path
    try:
        err = v.check_template(broken, {"sys_user": "ubuntu"}, out_dir, "bash")
    finally:
        v.REPO = orig_repo

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
    orig_repo = v.REPO
    v.REPO = tmp_path
    try:
        err = v.check_template(
            clean, {"sys_user": "ubuntu"}, out_dir, shutil.which("shellcheck")
        )
    finally:
        v.REPO = orig_repo

    assert err is None


def test_run_fails_closed_when_shellcheck_missing():
    # A missing shellcheck must FAIL the gate, not silently fall back to bash -n alone — the
    # whole point of failing loud instead of degrading (see module docstring / SHELL_STUB_OVERRIDES
    # design comment).
    assert v.run(print, which=lambda _name: None) == (0, 1)


def test_role_defaults_reach_the_render_context():
    # StubUndefined iterates empty, so the gate lints the artifacts sync script (almost all a `{%
    # for %}` body) only if the component's own defaults are in the context.
    path = v.COMPONENTS / "claude-code" / "templates" / "sync-artifacts.sh.j2"
    ctx = v.template_context(path)
    assert ctx["claude_code_artifacts_peer_sources"], ctx.get(
        "claude_code_artifacts_peer_sources"
    )
    assert "{{" not in render_shipped_text(path, ctx)


def test_a_host_components_templates_are_discovered():
    # A component glob that stopped matching would drop the Pi heartbeats from the gate with every
    # other template still checked.
    component = v.COMPONENTS / "pi-heartbeats" / "templates" / "pi-sd-health.sh.j2"
    assert component in v.discover_templates()


def test_component_defaults_and_host_reach_the_render_context():
    # A component has no role defaults file: its defaults are component.yml's `defaults:` map, and
    # its host is the one whose package lists it.
    path = v.COMPONENTS / "pi-heartbeats" / "templates" / "pi-recovery-health.sh.j2"
    ctx = v.template_context(path)
    assert ctx["optimize_pi_recovery_restart_enabled"] is True
    # wg-easy runs only on daniel-pi, so its compose_services entry proves the host layer is the
    # Pi's.
    assert any(c["name"] == "wg-easy" for c in ctx["compose_services"])


def test_hostvars_carries_every_hosts_inventory_address():
    # A `hostvars` stub replacing render_context's whole map would render `STUB` where the addresses
    # go in the etcd drill's fence.
    path = v.COMPONENTS / "hypervisor" / "templates" / "etcd-restore-drill-vm.sh.j2"
    text = render_shipped_text(path, v.template_context(path))
    box, pi = (host_vars(h)["server_ip"] for h in ("daniel-box", "daniel-pi"))
    assert f"https://{box}:6443/version" in text
    assert f"http://{pi}:51821" in text


def test_a_component_no_package_lists_has_no_host():
    assert v.component_host("no-such-component") is None


def test_an_override_beats_the_role_default():
    path = v.COMPONENTS / "claude-code" / "templates" / "sync-artifacts.sh.j2"
    ctx = v.template_context(
        path, overrides={"claude_code_artifacts_peer_dir": "/tmp/peer"}
    )
    assert ctx["claude_code_artifacts_peer_dir"] == "/tmp/peer"


def _include_joined_component(root, name, partial, body):
    """A component template whose `{% include %}` is valid only if the include keeps its newline."""
    component = root / "widget"
    (component / "templates").mkdir(parents=True)
    (component / "component.yml").write_text("{}\n")
    (component / "templates" / "head.j2").write_text(partial)
    tpl = component / "templates" / name
    tpl.write_text("{% include 'templates/head.j2' %}" + body)
    return tpl


def test_check_template_flags_a_script_the_package_would_ship_broken(tmp_path):
    """The package drops the include's last newline, so the comment swallows the `fi`."""
    tpl = _include_joined_component(
        tmp_path, "run.sh.j2", "#!/bin/bash\nif true; then # head\n", "echo ok; fi\n"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    err = v.check_template(tpl, {}, out_dir, None)
    assert err is not None and "bash -n" in err
