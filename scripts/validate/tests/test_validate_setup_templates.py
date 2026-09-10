"""Tests for validate.setup_templates — the setup-plane render guard (GitHub issue #1468).

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
# shape the guard covers: a YAML manifest, a Corefile, an env file, a unit, and a template
# outside the k3s role.
MUST_FIND = frozenset(
    {
        "roles/setup/k3s/templates/longhorn-b2-secret.yaml.j2",
        "roles/setup/k3s/templates/host-corefile.j2",
        "roles/setup/k3s/templates/kuma-push.env.j2",
        "roles/setup/gitops_deploy/templates/gitops-deploy.service.j2",
        "roles/setup/common/templates/resolv.conf.j2",
        "roles/setup/hypervisor/templates/staging-nwfilter.xml.j2",
    }
)


@pytest.fixture(scope="module")
def known():
    return v.secret_names() | v.runtime_vars() | v.inventory_names()


@pytest.fixture(scope="module")
def ctx():
    return {**v.BASE_CONTEXT, **v.ANSIBLE_RUNTIME_CONTEXT, **v.load_yaml(v.ALL_VARS)}


def _rel(path):
    return str(path.relative_to(v.ANSIBLE))


def test_the_census_finds_the_named_setup_templates():
    """Non-vacuity: the glob must return these by name, not merely return something."""
    found = {_rel(p) for p in v.discover_templates()}
    assert MUST_FIND <= found, f"discovery lost: {sorted(MUST_FIND - found)}"
    assert len(found) >= 70


def test_every_real_setup_template_renders_with_no_unresolved_variable(ctx, known):
    """The regression guard: the whole setup plane renders clean against the real tree."""
    bad = {}
    for tpl in v.discover_templates():
        role_ctx = {**ctx, **v.load_yaml(tpl.parents[1] / "defaults" / "main.yml")}
        problems = v.check_template(tpl, role_ctx, known)
        if problems:
            bad[_rel(tpl)] = problems
    assert not bad, f"setup templates failed to render clean: {bad}"


def test_an_undefined_variable_is_flagged(tmp_path, ctx, known):
    """The rejecting half, in the shape issue #1468 describes: a consumer left on an old name."""
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
        'runtime: "{{ k3s_longhorn_b2_region }}"\n'  # set_fact in a setup-plane task
        'inventory: "{{ domain }}"\n'  # group_vars/all.yml
        'magic: "{{ inventory_hostname }}"\n'  # Ansible-supplied
    )
    assert v.check_template(tpl, ctx, known) == []


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
    assert "k3s_longhorn_b2_region" in names  # ansible.builtin.set_fact
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
