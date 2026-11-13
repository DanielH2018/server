"""Tests for validate.setup_templates — the host component render guard.

Every rule here is an accept/reject pair, the convention
`scripts/validate/tests/test_validate_compose_templates.py` sets: a guard that fires on
everything and one that fires on nothing look identical from the passing side alone.

The census this guard runs over is a glob, so it also carries a non-vacuity assertion —
`MUST_FIND`, a frozenset of templates the discovery must return by name. Without it a rename
that emptied the glob would leave every test below passing over nothing.
"""

import pytest
import yaml

from lib import yaml_fast
from renderer.renderer_lib.environment import render_file
from renderer.renderer_lib.host_sources import confined_env
from validate import setup_templates as v

# Named members the glob must find. Renaming or moving one of these should fail HERE, with the
# member named, rather than silently shrinking the set every other test runs over. One per
# shape the guard covers: a YAML manifest, a Corefile, an env file, a unit, a libvirt filter, a
# sudoers drop-in, and templates of components with no `defaults/main.yml`.
MUST_FIND = frozenset(
    {
        "hosts/components/k3s-host-dns/templates/netplan-dns.yaml.j2",
        "hosts/components/k3s-host-dns/templates/host-corefile.j2",
        "hosts/components/claude-code/templates/claude-triage-config.env.j2",
        "hosts/components/claude-code/templates/claude-clone-sync.service.j2",
        "hosts/components/pi-resolver/templates/resolv.conf.j2",
        "hosts/components/nut-secondary/templates/host-upsmon.conf.j2",
        "hosts/components/hypervisor/templates/staging-nwfilter.xml.j2",
        "hosts/components/claude-code/templates/claude-rootq.sudoers.j2",
        "hosts/components/fail2ban/templates/fail2ban_homelab.conf.j2",
        "hosts/components/integrity-scans/templates/rkhunter.conf.local.j2",
    }
)


@pytest.fixture(scope="module")
def known():
    return v.secret_names() | v.inventory_names()


@pytest.fixture(scope="module")
def ctx():
    return v.render_context(
        v.COMPONENTS / "pi-resolver", overrides=v.HOST_RENDER_CONTEXT
    )


def _rel(path):
    return v.repo_relative(path)


def test_the_census_finds_the_named_component_templates():
    """Non-vacuity: the glob must return these by name, not merely return something."""
    found = {_rel(p) for p in v.discover_templates()}
    assert MUST_FIND <= found, f"discovery lost: {sorted(MUST_FIND - found)}"
    assert len(found) >= 106


def test_every_real_setup_template_renders_with_no_unresolved_variable(known):
    """The regression guard: every component template renders clean against the real tree."""
    bad = {}
    for tpl in v.discover_templates():
        problems = v.check_role_template(tpl, known)
        if problems:
            bad[_rel(tpl)] = problems
    assert not bad, f"component templates failed to render clean: {bad}"


def test_an_undefined_variable_is_flagged(tmp_path, ctx, known):
    """The rejecting half, in the shape a rename takes: a consumer left on an old name."""
    tpl = tmp_path / "role" / "templates" / "creds.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text('key: "{{ longhorn_b2_key_id_that_no_rename_left_behind }}"\n')
    problems = v.check_template(tpl, ctx, known)
    assert problems
    assert "longhorn_b2_key_id_that_no_rename_left_behind" in problems[0]


def test_a_defined_variable_is_clean(tmp_path, ctx, known):
    """The accepting half, over each source the tree supplies a name from."""
    tpl = tmp_path / "role" / "templates" / "creds.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text(
        'secret: "{{ longhorn_b2_key_id }}"\n'  # SOPS registry
        'inventory: "{{ domain }}"\n'  # group_vars/all.yml
        'magic: "{{ inventory_hostname }}"\n'  # Ansible-supplied
        'required: "{{ nut_monitor_password | mandatory }}"\n'  # the Ansible filter
    )
    assert v.check_template(tpl, ctx, known) == []


def test_a_default_that_will_not_expand_still_counts_as_defined(tmp_path, known):
    """Both halves: the component's own default is clean, a name nothing defines is flagged.

    `render_context` leaves out a default whose value names a filter the light environment
    lacks, so the render reads it as undefined while the component defines it.
    """
    role = tmp_path / "components" / "fixture"
    (role / "defaults").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "defaults" / "main.yml").write_text(
        'declared: "{{ x | no_such_filter }}"\n'
    )
    tpl = role / "templates" / "both.yaml.j2"
    tpl.write_text('a: "{{ declared }}"\nb: "{{ nothing_declares_this_name }}"\n')
    problems = v.check_role_template(tpl, known)
    assert len(problems) == 1
    assert "nothing_declares_this_name" in problems[0]


def test_broken_yaml_is_flagged(tmp_path, ctx, known):
    tpl = tmp_path / "role" / "templates" / "broken.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text("a:\n\t- broken: : :\n")
    problems = v.check_template(tpl, ctx, known)
    assert problems
    assert "invalid YAML" in problems[0]


def test_a_non_yaml_template_is_not_parsed_as_yaml(tmp_path, ctx, known):
    """A Corefile or a libvirt XML is rendered, never YAML-parsed — it would never parse."""
    tpl = tmp_path / "role" / "templates" / "host-corefile.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text(".:53 {\n    forward . 1.1.1.1\n}\n")
    assert v.check_template(tpl, ctx, known) == []


def test_a_jinja_syntax_error_is_flagged(tmp_path, ctx, known):
    tpl = tmp_path / "role" / "templates" / "syntax.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text("a: {% if %}\n")
    problems = v.check_template(tpl, ctx, known)
    assert problems
    assert "render error" in problems[0]


def test_a_host_render_name_is_allowed_and_an_unknown_name_is_not():
    known = frozenset({"defined_somewhere"})
    assert v.is_allowed_undefined("inventory_hostname", known)
    assert not v.is_allowed_undefined("ansible_facts.hostname", known)
    assert not v.is_allowed_undefined("ansible_managed", known)
    assert v.is_allowed_undefined("defined_somewhere", known)
    assert not v.is_allowed_undefined("typo_var", known)


def test_the_secret_registry_is_read_by_name_only():
    """Names, never values — the registry is plaintext precisely because it holds no value."""
    names = v.secret_names()
    assert "longhorn_b2_key_id" in names
    assert len(names) >= 50


def test_a_borrowing_component_supplies_its_defaults_and_secret_values(tmp_path):
    """Both halves: the borrowed component gets the names, another component does not."""
    component = tmp_path / "widget"
    component.mkdir()
    (component / "component.yml").write_text(
        "component: widget-owner\n"
        "defaults: {widget_agents_present: [a]}\n"
        "outputs:\n"
        "  - {src: templates/x.j2, keys: [widget_token]}\n"
    )
    supplied = v.component_supplied("widget-owner", tmp_path)
    assert supplied == {"widget_agents_present", "widget_token_value"}
    assert v.component_supplied("other", tmp_path) == frozenset()


def test_the_claude_code_templates_get_the_names_its_component_supplies():
    supplied = v.component_supplied("claude-code")
    assert {"claude_code_agents_present", "claude_code_rc_unit"} <= supplied


def _component_template(root, text: str, defaults: str):
    component = root / "widget"
    (component / "templates").mkdir(parents=True)
    (component / "defaults").mkdir()
    (component / "component.yml").write_text("defaults: {widget_unit: w.service}\n")
    (component / "defaults" / "main.yml").write_text(defaults)
    (component / "templates" / "pins.j2").write_text("{% set port = 1 %}\n")
    tpl = component / "templates" / "widget.conf.j2"
    tpl.write_text(text)
    return tpl


def test_a_components_template_is_discovered_and_its_shell_template_is_not(tmp_path):
    tpl = _component_template(tmp_path, "x\n", "widget_on: true\n")
    shell = tpl.parent / "run.sh.j2"
    shell.write_text("echo\n")
    found = v.discover_templates(tmp_path)
    assert tpl in found
    assert shell not in found


def test_a_component_template_reading_its_defaults_and_imports_is_clean(
    tmp_path, known
):
    tpl = _component_template(
        tmp_path,
        "{% import 'templates/pins.j2' as pins %}{{ widget_on }} {{ widget_unit }}\n",
        "widget_on: true\n",
    )
    assert v.check_role_template(tpl, known, tmp_path) == []


def test_a_component_template_reading_a_name_nothing_defines_is_flagged(
    tmp_path, known
):
    tpl = _component_template(tmp_path, "{{ widget_gone }}\n", "widget_on: true\n")
    problems = v.check_role_template(tpl, known, tmp_path)
    assert problems and "widget_gone" in problems[0]


def _include_component(root):
    """A component whose YAML is valid only if an `{% include %}` keeps its last newline."""
    component = root / "widget"
    (component / "templates").mkdir(parents=True)
    (component / "component.yml").write_text("{}\n")
    (component / "templates" / "item.j2").write_text("x\n")
    tpl = component / "templates" / "widget.yaml.j2"
    tpl.write_text("a: {% include 'templates/item.j2' %}b: 1\n")
    return tpl


def test_a_render_the_package_would_ship_broken_is_flagged(tmp_path):
    """The guard renders through the package's environment, so it judges the bytes that ship (#8182).

    The package's `strict_env` drops an include's last newline, which joins the two keys
    into invalid YAML; an environment that keeps it renders valid YAML the package never ships.
    """
    tpl = _include_component(tmp_path)
    shipped = render_file(confined_env(tpl.parents[1]), "templates/widget.yaml.j2", {})
    with pytest.raises(yaml.YAMLError):
        yaml_fast.safe_load(shipped)
    problems = v.check_template(tpl, {}, frozenset())
    assert any("invalid YAML" in p for p in problems), problems
