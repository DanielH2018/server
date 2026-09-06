"""Tests for scripts/validate/shell_templates.py and the scripts/lib/ modules it composes.

This module keeps the validator machinery: template discovery, the render/lint/syntax passes,
the shellcheck batch attribution and `main`'s fail-closed behaviour. The cron rules the
validator composes live in `test_shell_template_cron_rules.py`, and the rendered
longhorn-backup-health shim in `test_backup_health_shim.py`.

Run: uv run pytest scripts/validate/tests/test_validate_shell_templates.py
"""

import shutil

import pytest
from validate import shell_templates as v
from lib import ansible_jinja_compat as ajc
from lib import shell_lint as sl


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
    assert ajc.ansible_bool(value) is expected


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
        "github-interaction-limit.sh.j2",
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
    assert ajc.ansible_search("172.64.0.0/13", ":") is False
    assert ajc.ansible_search("2400:cb00::/32", ":") is True
    assert ajc.ansible_search("ABC", "abc", ignorecase=True) is True
    assert ajc.ansible_search("ABC", "abc", ignorecase=False) is False


def test_bash_syntax_check_catches_unmatched_quote(tmp_path):
    # The 2026-07-01 kopia bug class (ansible/roles/containers/archive/kopia/files/maintenance-check.sh):
    # an apostrophe broke bash's own quote parsing inside a single-quoted block. Reproduce the
    # shape here — a stray unmatched single quote — and confirm bash -n rejects it.
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
    # A var with no BASE_CONTEXT/SHELL_STUB_OVERRIDES/all.yml entry falls back to StubUndefined
    # ("STUB") rather than aborting the render.
    tpl = tmp_path / "sample.sh.j2"
    tpl.write_text("#!/bin/bash\necho {{ some_never_defined_var }}\n")
    rendered = sl.render_template(tpl, {})
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
            clean, {"sys_user": "ubuntu"}, out_dir, shutil.which("shellcheck"), {}
        )
    finally:
        v.ANSIBLE = orig_ansible

    assert err is None


def test_main_fails_closed_when_shellcheck_missing():
    # A missing shellcheck must FAIL the gate, not silently fall back to bash -n alone — the
    # whole point of failing loud instead of degrading (see module docstring / SHELL_STUB_OVERRIDES
    # design comment).
    assert v.main(which=lambda _name: None) == 1
