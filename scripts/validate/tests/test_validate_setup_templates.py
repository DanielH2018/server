"""Tests for validate.setup_templates — the setup-plane render guard.

Every rule here is an accept/reject pair, the convention
`scripts/validate/tests/test_validate_compose_templates.py` sets: a guard that fires on
everything and one that fires on nothing look identical from the passing side alone.

The census this guard runs over is a glob, so it also carries a non-vacuity assertion —
`MUST_FIND`, a frozenset of templates the discovery must return by name. Without it a rename
that emptied the glob would leave every test below passing over nothing.
"""

import pytest

from validate import setup_templates as v

# Named members the glob must find. Renaming or moving one of these should fail HERE, with the
# member named, rather than silently shrinking the set every other test runs over. One per
# shape the guard covers: a YAML manifest, a Corefile, an env file, a unit, a template
# outside the k3s role, and a template a role-shaped host component holds.
MUST_FIND = frozenset(
    {
        "roles/setup/k3s/templates/registries.yaml.j2",
        "roles/setup/k3s/templates/host-corefile.j2",
        "roles/setup/gitops_deploy/templates/config.env.j2",
        "roles/setup/gitops_deploy/templates/gitops-deploy.service.j2",
        "roles/setup/common/templates/resolv.conf.j2",
        "roles/setup/nut_host/templates/host-upsmon.conf.j2",
        "roles/setup/hypervisor/templates/staging-nwfilter.xml.j2",
        "hosts/components/claude-code/templates/claude-rootq.sudoers.j2",
    }
)


@pytest.fixture(scope="module")
def known():
    return v.secret_names() | v.runtime_vars() | v.inventory_names()


@pytest.fixture(scope="module")
def ctx():
    return v.render_context(v.SETUP / "common", overrides=v.ANSIBLE_RUNTIME_CONTEXT)


def _rel(path):
    return v.repo_relative(path)


def test_the_census_finds_the_named_setup_templates():
    """Non-vacuity: the glob must return these by name, not merely return something."""
    found = {_rel(p) for p in v.discover_templates()}
    assert MUST_FIND <= found, f"discovery lost: {sorted(MUST_FIND - found)}"
    assert len(found) >= 110


def test_every_real_setup_template_renders_with_no_unresolved_variable(known):
    """The regression guard: the whole setup plane renders clean against the real tree."""
    bad = {}
    for tpl in v.discover_templates():
        problems = v.check_role_template(tpl, known)
        if problems:
            bad[_rel(tpl)] = problems
    assert not bad, f"setup templates failed to render clean: {bad}"


def test_an_undefined_variable_is_flagged(tmp_path, ctx, known):
    """The rejecting half, in the shape a rename takes: a consumer left on an old name."""
    tpl = tmp_path / "role" / "templates" / "creds.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text('key: "{{ longhorn_b2_key_id_that_no_rename_left_behind }}"\n')
    problems = v.check_template(tpl, ctx, known)
    assert problems
    assert "longhorn_b2_key_id_that_no_rename_left_behind" in problems[0]


def test_a_defined_variable_is_clean(tmp_path, ctx, known):
    """The accepting half, over each of the four sources a name may legitimately come from."""
    tpl = tmp_path / "role" / "templates" / "creds.yaml.j2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text(
        'secret: "{{ longhorn_b2_key_id }}"\n'  # SOPS registry
        'runtime: "{{ hypervisor_staging_net_uuid }}"\n'  # set_fact in a setup-plane task
        'inventory: "{{ domain }}"\n'  # group_vars/all.yml
        'magic: "{{ inventory_hostname }}"\n'  # Ansible-supplied
        'required: "{{ ups_secondary_push_token | mandatory }}"\n'  # the Ansible filter
    )
    assert v.check_template(tpl, ctx, known) == []


def test_a_default_that_will_not_expand_still_counts_as_defined(tmp_path, known):
    """Both halves: the role's own default is clean, a name nothing defines is still flagged.

    `render_context` leaves out a default whose value names a filter the light environment
    lacks, so the render reads it as undefined while the role defines it.
    """
    role = tmp_path / "roles" / "setup" / "fixture"
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


def test_runtime_vars_reads_both_set_fact_spellings():
    """`ansible.builtin.set_fact` is what this repo writes; matching the bare name found none.

    Both names in the assertion: this is a derivation replacing no list, so the way it fails is
    by returning a set narrower than the tree, which only a named member catches.
    """
    names = v.runtime_vars()
    assert "hypervisor_staging_net_uuid" in names  # ansible.builtin.set_fact
    assert (
        "common_resolver_nameservers" in names
    )  # ansible.builtin.set_fact, another role
    assert len(names) >= 100


def test_an_ansible_magic_var_is_allowed_and_an_unknown_name_is_not():
    known = frozenset({"defined_somewhere"})
    assert v.is_allowed_undefined("ansible_facts.hostname", known)
    assert v.is_allowed_undefined("inventory_hostname", known)
    assert v.is_allowed_undefined("defined_somewhere", known)
    assert not v.is_allowed_undefined("typo_var", known)


def test_the_secret_registry_is_read_by_name_only():
    """Names, never values — the registry is plaintext precisely because it holds no value."""
    names = v.secret_names()
    assert "longhorn_b2_key_id" in names
    assert len(names) >= 50


def test_a_component_supplies_its_defaults_and_secret_values_to_its_roles_templates(
    tmp_path,
):
    """Both halves: the component's role gets the names, another role does not."""
    component = tmp_path / "widget"
    component.mkdir()
    (component / "component.yml").write_text(
        "role: widget_role\n"
        "defaults: {widget_agents_present: [a]}\n"
        "outputs:\n"
        "  - {src: templates/x.j2, keys: [widget_token]}\n"
    )
    supplied = v.component_supplied("widget_role", tmp_path)
    assert supplied == {"widget_agents_present", "widget_token_value"}
    assert v.component_supplied("other_role", tmp_path) == frozenset()


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


def test_a_role_shaped_components_template_is_discovered_and_a_bare_one_is_not(
    tmp_path,
):
    tpl = _component_template(tmp_path, "x\n", "widget_on: true\n")
    (tmp_path / "bare" / "templates").mkdir(parents=True)
    (tmp_path / "bare" / "templates" / "y.j2").write_text("y\n")
    found = v.discover_templates(tmp_path / "no-setup", tmp_path)
    assert tpl in found
    assert tmp_path / "bare" / "templates" / "y.j2" not in found


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
