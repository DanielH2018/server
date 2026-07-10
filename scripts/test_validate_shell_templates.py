"""Tests for scripts/validate_shell_templates.py — the render-then-lint guard for Jinja-templated
shell scripts (*.sh.j2) that the prek bash-syntax-check / shellcheck hooks can't see (identify
tags a `.sh.j2` as {jinja, text}, never `shell`).
"""

import validate_shell_templates as v


def test_all_real_shell_templates_render_and_lint_clean():
    # The regression guard: every *.sh.j2 under ansible/roles/ must render with stubbed vars to
    # a script that passes both `bash -n` and shellcheck. Mirrors the sibling validators'
    # real-render tests (validate_compose_templates.test_real_templates_render_clean,
    # validate_config_templates.test_all_real_config_templates_render_to_valid_yaml).
    assert v.main() == 0


def test_discover_templates_finds_the_known_set():
    # Pin the expected set so a template silently going missing (typo'd rename, moved role) is
    # caught, not just "fewer templates checked" sliding by unnoticed.
    names = {p.name for p in v.discover_templates()}
    assert names == {
        "entrypoint.sh.j2",
        "crowdsec-update-home-allowlist.sh.j2",
        "docker-user-rules.sh.j2",
        "secret-rotate.sh.j2",
        "secret-rotation-audit.sh.j2",
        "pi-sd-health.sh.j2",
        "pi-recovery-health.sh.j2",
        "pull-pi-peers.sh.j2",
        "portainer-agent-firewall.sh.j2",
        "autofix-disk-prune.sh.j2",
    }


def test_discover_templates_excludes_vendored_collections():
    # ansible/collections/ ships its own *.sh.j2 test fixtures (community.general) — not ours to
    # lint, same exclusion pytest's testpaths / ruff's extend-exclude already apply.
    assert all("collections" not in p.parts for p in v.discover_templates())


def test_ansible_search_test_mirrors_the_real_jinja_test():
    # docker-user-rules.sh.j2 renders via `cloudflare_ips | reject('search', ':')` to split
    # IPv4 from IPv6 ranges — vanilla Jinja2 has no `search` test at all (TemplateRuntimeError
    # without this), so this pins the regex-search (not full-match) semantics that filter relies on.
    assert v._ansible_search("172.64.0.0/13", ":") is False
    assert v._ansible_search("2400:cb00::/32", ":") is True
    assert v._ansible_search("ABC", "abc", ignorecase=True) is True
    assert v._ansible_search("ABC", "abc", ignorecase=False) is False


def test_bash_syntax_check_catches_unmatched_quote(tmp_path):
    # The 2026-07-01 kopia bug class (ansible/roles/containers/kopia/files/maintenance-check.sh):
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
        err = v.check_template(broken, {"sys_user": "ubuntu"}, out_dir, "bash")
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
            clean, {"sys_user": "ubuntu"}, out_dir, v.shutil.which("shellcheck")
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
