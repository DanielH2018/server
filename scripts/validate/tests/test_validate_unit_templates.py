"""Tests for scripts/validate/unit_templates.py — the render-then-`systemd-analyze verify` guard
for `*.service.j2` / `*.timer.j2` templates that neither `shell_templates.py` nor
`k8s_manifests.py` covers (GitHub issue #948).
"""

import shutil

import pytest
from validate import unit_templates as v

# Applied only to the tests that actually shell out to systemd-analyze — a module-wide skip
# would also hide test_main_fails_closed_when_systemd_analyze_missing (which monkeypatches
# `which` and needs no real binary) and every pure discovery/render test below.
requires_systemd_analyze = pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze not on PATH",
)

GITOPS_DEPLOY_UNIT = (
    v.ROLES / "setup" / "gitops_deploy" / "templates" / "gitops-deploy.service.j2"
)


def _write_unit(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body)
    return path


@requires_systemd_analyze
def test_systemd_verify_is_clean_for_a_well_formed_service(tmp_path):
    unit = _write_unit(
        tmp_path,
        "clean.service",
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n",
    )
    assert v.systemd_verify(unit, "systemd-analyze") is None


@requires_systemd_analyze
def test_systemd_verify_is_flagged_for_a_typo_d_key(tmp_path):
    # The measured shape from the issue: a typo'd directive key that systemd itself ignores
    # with a warning and continues loading — the class no exit-status check can see.
    unit = _write_unit(
        tmp_path,
        "typo.service",
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        "SuccessExitStatuss=75\n",
    )
    err = v.systemd_verify(unit, "systemd-analyze")
    assert err is not None
    assert "Unknown key name" in err
    assert "SuccessExitStatuss" in err


@requires_systemd_analyze
def test_systemd_verify_is_flagged_for_a_bad_oncalendar(tmp_path):
    unit = _write_unit(
        tmp_path,
        "badcal.timer",
        "[Unit]\nDescription=test\n\n[Timer]\nOnCalendar=not-a-calendar-spec\n\n"
        "[Install]\nWantedBy=timers.target\n",
    )
    err = v.systemd_verify(unit, "systemd-analyze")
    assert err is not None
    assert "Failed to parse" in err


@requires_systemd_analyze
def test_systemd_verify_ignores_a_missing_execstart_binary(tmp_path):
    # A structural check only: this must not flag an ExecStart binary that doesn't exist on
    # the render host — that line ("Command ... is not executable") carries no "path:line:"
    # prefix, so it never matches the attribution check.
    unit = _write_unit(
        tmp_path,
        "notreal.service",
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\n"
        "ExecStart=/usr/local/bin/notreal\n",
    )
    assert v.systemd_verify(unit, "systemd-analyze") is None


@requires_systemd_analyze
def test_systemd_verify_ignores_bare_invalid_without_a_line_attribution(tmp_path):
    # CLAUDE.md's own rule for this guard family: no bare "Invalid" substring match — it is
    # never produced by systemd-analyze here and is the broadest possible false-positive net.
    unit = _write_unit(
        tmp_path,
        "clean2.service",
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n",
    )
    assert v.systemd_verify(unit, "systemd-analyze") is None


def test_systemd_verify_ignores_the_exit_status(tmp_path):
    # Uses `false`, not systemd-analyze itself, so it needs no real binary on PATH: `false`
    # always exits 1 and prints nothing systemd-analyze-shaped — if systemd_verify() read the
    # exit code it would flag every clean unit run through this stand-in. It must not.
    unit = _write_unit(
        tmp_path,
        "clean3.service",
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n",
    )
    assert v.systemd_verify(unit, "false") is None


def test_discover_templates_finds_the_known_set():
    # Pin the expected corpus so a template silently going missing (typo'd rename, moved role)
    # is caught rather than "fewer templates checked" sliding by unnoticed.
    names = frozenset(p.name for p in v.discover_templates())
    assert {
        "gitops-deploy.service.j2",
        "gitops-deploy.timer.j2",
        "renovate-agent.timer.j2",
    } <= names
    assert len(names) == 19


def test_discover_templates_excludes_archive():
    assert not any(
        "archive" in p.relative_to(v.ROLES).parts for p in v.discover_templates()
    )
    happy_daemon = (
        v.ROLES
        / "containers"
        / "archive"
        / "happy"
        / "templates"
        / "happy-daemon.service.j2"
    )
    assert happy_daemon.exists(), (
        "fixture assumption broke — the archived template moved"
    )
    assert happy_daemon not in v.discover_templates()


def test_owning_role_defaults_resolves_the_role_directory():
    defaults = v.owning_role_defaults(GITOPS_DEPLOY_UNIT)
    assert defaults == v.ROLES / "setup" / "gitops_deploy" / "defaults" / "main.yml"
    assert defaults.is_file()


def test_render_context_layers_the_owning_roles_real_defaults():
    ctx = v.render_context(GITOPS_DEPLOY_UNIT)
    # A real value, not "STUB" — bare StubUndefined renders OnUnitActiveSec as the literal
    # string "STUB", which is what made the sibling timer red before this layering existed.
    assert ctx["gitops_deploy_tick_interval"] == "10min"


def test_render_context_still_stubs_a_var_no_default_carries():
    ctx = v.render_context(GITOPS_DEPLOY_UNIT)
    assert "inventory_hostname" not in ctx


@requires_systemd_analyze
def test_check_template_catches_the_real_typo_class(tmp_path):
    role = tmp_path / "roles" / "setup" / "fixture"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text("---\n")
    broken = role / "templates" / "broken.service.j2"
    broken.write_text(
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        "SuccessExitStatuss=75\n"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig_ansible = v.ANSIBLE
    v.ANSIBLE = tmp_path
    try:
        err = v.check_template(broken, {}, out_dir, "systemd-analyze")
    finally:
        v.ANSIBLE = orig_ansible
    assert err is not None
    assert "systemd-analyze verify" in err


@requires_systemd_analyze
def test_check_template_passes_a_clean_render(tmp_path):
    role = tmp_path / "roles" / "setup" / "fixture"
    (role / "templates").mkdir(parents=True)
    clean = role / "templates" / "clean.service.j2"
    clean.write_text(
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\n"
        "ExecStart=/usr/local/bin/notreal\n"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig_ansible = v.ANSIBLE
    v.ANSIBLE = tmp_path
    try:
        err = v.check_template(clean, {}, out_dir, "systemd-analyze")
    finally:
        v.ANSIBLE = orig_ansible
    assert err is None


def test_main_fails_closed_when_systemd_analyze_missing(monkeypatch):
    monkeypatch.setattr(v.shutil, "which", lambda name: None)
    assert v.main() == 1


# ── issue #948 sibling gap: 50-gitops-deploy.rules.j2 (polkit JS) is content-checked by
# test_gitops_manual_trigger.py, but nothing checked its JavaScript syntax.


requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH"
)


@requires_node
def test_check_polkit_rules_passes_the_real_template(tmp_path):
    err = v.check_polkit_rules(tmp_path, "node")
    assert err is None


@requires_node
def test_check_polkit_rules_catches_unbalanced_braces(tmp_path, monkeypatch):
    # The measured shape: a stray brace that survives Jinja (no `{{`/`{%`/`{#`) but leaves the
    # rendered JS structurally broken — the class `node --check` exists to catch.
    broken = tmp_path / "50-gitops-deploy.rules.j2"
    broken.write_text(
        "polkit.addRule(function (action, subject) {\n    return undefined;\n);"
    )
    monkeypatch.setattr(v, "RULES_TEMPLATE", broken)
    err = v.check_polkit_rules(tmp_path, "node")
    assert err is not None
    assert "node --check" in err


def test_main_skips_the_polkit_check_when_node_missing(monkeypatch):
    real_which = v.shutil.which
    monkeypatch.setattr(
        v.shutil, "which", lambda name: None if name == "node" else real_which(name)
    )
    # A missing node must not fail the whole run closed — only a missing systemd-analyze does.
    assert v.main() == 0


def test_main_fails_when_node_check_flags_the_rendered_rule(tmp_path, monkeypatch):
    broken = tmp_path / "50-gitops-deploy.rules.j2"
    broken.write_text(
        "polkit.addRule(function (action, subject) {\n    return undefined;\n);"
    )
    monkeypatch.setattr(v, "RULES_TEMPLATE", broken)
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    assert v.main() == 1


@requires_systemd_analyze
def test_all_real_unit_templates_render_and_verify_clean():
    # The regression guard, mirroring shell_templates.test_all_real_shell_templates_render_and_lint_clean:
    # every live *.service.j2 / *.timer.j2 must render with StubUndefined + its owning role's real
    # defaults to a unit systemd-analyze reports no attributed diagnostic for.
    assert v.main() == 0
