"""Tests for scripts/validate/unit_templates.py — the render-then-`systemd-analyze verify` guard
for `*.service.j2` / `*.timer.j2` templates that neither `shell_templates.py` nor
`k8s_manifests.py` covers.
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
    assert "Unknown key" in err
    assert "SuccessExitStatuss" in err


# systemd v257 reworded the unknown-key diagnostic, so the wording a runner image prints
# depends on which systemd it ships: 255 on ubuntu-24.04, 259 on ubuntu-26.04. This is the
# half of the red proof that runs on every image — the test above shells out to whichever
# systemd-analyze is on PATH and can only ever exercise one wording per host.
@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        pytest.param(
            "/tmp/typo.service:6: Unknown key name 'SuccessExitStatuss' in section "
            "'Service', ignoring.",
            True,
            id="unknown-key-systemd-255",
        ),
        pytest.param(
            "/tmp/typo.service:6: Unknown key 'SuccessExitStatuss' in section [Service], "
            "ignoring.",
            True,
            id="unknown-key-systemd-259",
        ),
        pytest.param(
            "notreal.service: Command /usr/local/bin/notreal is not executable: "
            "No such file or directory",
            False,
            id="missing-execstart-binary",
        ),
    ],
)
def test_fail_message_matches_every_unknown_key_wording(line, flagged):
    assert bool(v._FAIL_MESSAGE.search(line)) is flagged


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
        "deploy-ui.service.j2",
        "homelab-apt-repo.service.j2",
        "gitops-deploy.service.j2",
        "gitops-deploy.timer.j2",
        "renovate-agent.timer.j2",
        "renovate-agent-land@.service.j2",
        "claude-land@.service.j2",
        "claude-clone-sync.service.j2",
        "claude-dotfiles-sync.service.j2",
        "node_exporter.service.j2",
        "kuma-check.service.j2",
        "kuma-check-boot.service.j2",
        "kuma-check.timer.j2",
        "unit-failure-alert.service.j2",
        "worktree-holders.socket.j2",
        "worktree-holders@.service.j2",
    } <= names
    assert len(names) == 29


def test_unit_context_layers_the_owning_roles_real_defaults():
    ctx = v.unit_context(GITOPS_DEPLOY_UNIT)
    # A real value, not "STUB" — bare StubUndefined renders OnUnitActiveSec as the literal
    # string "STUB", which is what made the sibling timer red before this layering existed.
    assert ctx["gitops_deploy_tick_interval"] == "10min"


def test_unit_context_still_stubs_a_var_no_default_carries():
    ctx = v.unit_context(GITOPS_DEPLOY_UNIT)
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


# ── sibling gap: the polkit rules (*.rules.j2, JavaScript) are content-checked by their roles'
# tests, but nothing else checks their JavaScript syntax.


requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH"
)

# Every rule a host runs. A rule renamed or moved out of a role's templates/ drops out of the
# glob, and the check would pass over the rules that remain.
KNOWN_RULES = frozenset(
    {
        "50-gitops-deploy.rules.j2",
        "50-renovate-agent-land.rules.j2",
        "50-claude-land.rules.j2",
    }
)


def test_discover_rules_finds_every_known_rule():
    assert KNOWN_RULES <= {p.name for p in v.discover_rules()}


@requires_node
@pytest.mark.parametrize("name", sorted(KNOWN_RULES))
def test_check_polkit_rule_passes_each_real_rule(tmp_path, name):
    (rule,) = [p for p in v.discover_rules() if p.name == name]
    assert v.check_polkit_rule(rule, tmp_path, "node") is None


def _broken_rule(tmp_path):
    # The measured shape: a stray brace that survives Jinja (no `{{`/`{%`/`{#`) but leaves the
    # rendered JS structurally broken — the class `node --check` exists to catch.
    role = tmp_path / "roles" / "setup" / "fixture"
    (role / "templates").mkdir(parents=True)
    broken = role / "templates" / "50-fixture.rules.j2"
    broken.write_text(
        "polkit.addRule(function (action, subject) {\n    return undefined;\n);"
    )
    return broken


@requires_node
def test_check_polkit_rule_catches_unbalanced_braces(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    err = v.check_polkit_rule(_broken_rule(tmp_path), out_dir, "node")
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
    broken = _broken_rule(tmp_path)
    monkeypatch.setattr(v, "discover_rules", lambda: [broken])
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    assert v.main() == 1


@requires_systemd_analyze
def test_all_real_unit_templates_render_and_verify_clean():
    # The regression guard, mirroring shell_templates.test_all_real_shell_templates_render_and_lint_clean:
    # every live *.service.j2 / *.timer.j2 must render with StubUndefined + its owning role's real
    # defaults to a unit systemd-analyze reports no attributed diagnostic for.
    assert v.main() == 0


# ── the host packages under hosts/, rendered by the host renderer ─────────────────────

PI_DROPIN = "[Timer]\nOnBootSec=\nOnUnitActiveSec=\nOnCalendar=*-*-* 03:00:00\n"


def _hosts_tree(tmp_path, dropin: str = PI_DROPIN, component_unit: str | None = None):
    """A hosts/ tree holding the real shared updater and a fixture daniel-pi package.

    The Pi package ships a drop-in over the shared `homelab-updater.timer`, the shape H3.6's
    real package takes, and lists a fixture component when `component_unit` is given.
    """
    root = tmp_path / "hosts"
    (root / "packages").mkdir(parents=True)
    (root / "components").mkdir()
    (root / "packages" / "homelab-updater").symlink_to(
        v.HOSTS / "packages" / "homelab-updater"
    )
    (root / "lib").symlink_to(v.HOSTS / "lib")
    pi = root / "daniel-pi"
    (pi / "units").mkdir(parents=True)
    (pi / "units" / "50-homelab.conf").write_text(dropin)
    spec = (
        "files:\n  - {src: units/50-homelab.conf, "
        "dst: /usr/lib/systemd/system/homelab-updater.timer.d/50-homelab.conf}\n"
    )
    if component_unit is not None:
        component = root / "components" / "fixture"
        (component / "units").mkdir(parents=True)
        (component / "units" / "homelab-fixture.service").write_text(component_unit)
        (component / "component.yml").write_text(
            "files:\n  - {src: units/homelab-fixture.service, "
            "dst: /usr/lib/systemd/system/homelab-fixture.service}\n"
        )
        spec = "components: [fixture]\n" + spec
    (pi / "package.yml").write_text(spec)
    return root


def _check_hosts(tmp_path, root):
    lines = []
    checked, failures = v.check_host_units(
        root, tmp_path / "out", "systemd-analyze", lines.append
    )
    return checked, failures, lines


@requires_systemd_analyze
def test_host_units_with_the_pis_updater_dropin_are_clean(tmp_path):
    checked, failures, lines = _check_hosts(tmp_path, _hosts_tree(tmp_path))
    # The shared updater's pair in the `shared` group, then again under the Pi's drop-in.
    assert (checked, failures) == (4, 0)
    assert "  [ok]   hosts/daniel-pi: homelab-updater.timer" in lines
    dropin = (
        tmp_path / "out/hosts/daniel-pi/homelab-updater.timer.d/50-homelab.conf"
    ).read_text()
    assert dropin == PI_DROPIN


@requires_systemd_analyze
def test_host_units_typo_in_the_dropin_over_the_shared_timer_is_flagged(
    tmp_path, capsys
):
    root = _hosts_tree(tmp_path, dropin="[Timer]\nRandomizedDelaySecc=5m\n")
    _, failures, _ = _check_hosts(tmp_path, root)
    assert failures == 1
    err = capsys.readouterr().err
    assert "hosts/daniel-pi: homelab-updater.timer" in err
    assert "50-homelab.conf:2: Unknown key" in err


@requires_systemd_analyze
def test_host_units_typo_in_a_component_unit_is_flagged(tmp_path, capsys):
    unit = (
        "[Unit]\nDescription=test\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        "SuccessExitStatuss=75\n"
    )
    _, failures, _ = _check_hosts(tmp_path, _hosts_tree(tmp_path, component_unit=unit))
    assert failures == 1
    assert "homelab-fixture.service" in capsys.readouterr().err


@requires_systemd_analyze
def test_host_units_without_the_named_member_is_flagged(tmp_path, capsys):
    root = tmp_path / "hosts"
    (root / "packages").mkdir(parents=True)
    checked, failures, _ = _check_hosts(tmp_path, root)
    assert (checked, failures) == (0, 1)
    assert f"no render emitted {v.HOST_NAMED_MEMBER}" in capsys.readouterr().err


def test_host_unit_groups_render_every_shipped_unit_of_the_tree(tmp_path):
    """The named members: the shared updater's pair and the unlisted reconcile component."""
    groups = dict(v.host_unit_groups(v.HOSTS, tmp_path))
    units = {unit for package in groups["shared"] for unit in package.units()}
    assert {
        "homelab-updater.service",
        "homelab-updater.timer",
        "homelab-reconcile.service",
    } <= units
