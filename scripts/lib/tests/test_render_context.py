"""The render context every validator builds, in Ansible's variable precedence.

Each test lays out a throwaway inventory and role tree under `tmp_path` and hands the render an
`Inventory` pointing at it, so a precedence rule is checked against a collision the real tree
does not carry.

Run: uv run pytest scripts/lib/tests/test_render_context.py
"""

from pathlib import Path

import pytest
import yaml

from lib import render_context as rc


@pytest.fixture
def tree(tmp_path: Path):
    """A writer for `<tmp>/roles/<plane>/<role>` defaults, all.yml and host_vars files.

    Each call returns the template path and the `Inventory` that points at the tree.
    """
    inventory = rc.Inventory(
        all_vars=tmp_path / "all.yml",
        host_vars=tmp_path / "host_vars",
        plane_hosts={"k8s": "box"},
    )
    inventory.host_vars.mkdir()

    def write(
        defaults: dict,
        group: dict | None = None,
        hosts: dict[str, dict] | None = None,
        plane: str = "setup",
    ) -> tuple[Path, rc.Inventory]:
        role = tmp_path / "roles" / plane / "fixture"
        (role / "defaults").mkdir(parents=True, exist_ok=True)
        (role / "templates").mkdir(exist_ok=True)
        (role / "defaults" / "main.yml").write_text(yaml.safe_dump(defaults))
        inventory.all_vars.write_text(yaml.safe_dump(group or {}))
        for name, values in (hosts or {}).items():
            (inventory.host_vars / f"{name}.yml").write_text(yaml.safe_dump(values))
        return role / "templates" / "x.j2", inventory

    return write


def test_an_inventory_value_beats_a_role_default_of_the_same_name(tree):
    # Role defaults are Ansible's weakest real layer. The k8s validator's old order put them
    # over the inventory, which renders a value no deploy produces.
    template, inv = tree({"shared": "default"}, group={"shared": "inventory"})
    assert rc.render_context(template, inventory=inv)["shared"] == "inventory"


def test_a_role_default_with_no_inventory_key_of_its_name_survives(tree):
    template, inv = tree({"own": "default"}, group={"other": "inventory"})
    ctx = rc.render_context(template, inventory=inv)
    assert (ctx["own"], ctx["other"]) == ("default", "inventory")


def test_a_role_default_beats_a_base_stub(tree):
    # `BASE_CONTEXT` holds fallbacks for values no plaintext file carries, so any real file
    # outranks it. The shell validator's old order laid the stubs over the defaults.
    template, inv = tree({"domain": "real.example"})
    assert rc.render_context(template, inventory=inv)["domain"] == "real.example"


def test_the_k8s_plane_renders_with_its_hosts_vars(tree):
    template, inv = tree({}, group={"port": 1}, hosts={"box": {"port": 2}}, plane="k8s")
    assert rc.render_context(template, inventory=inv)["port"] == 2


def test_a_setup_template_renders_with_no_hosts_vars(tree):
    # A setup role runs on several hosts, so no one host's values belong in its render.
    template, inv = tree(
        {}, group={"port": 1}, hosts={"box": {"port": 2}}, plane="setup"
    )
    assert rc.render_context(template, inventory=inv)["port"] == 1


def test_a_named_host_beats_group_vars_on_any_plane(tree):
    template, inv = tree(
        {}, group={"port": 1}, hosts={"pi": {"port": 3}}, plane="containers"
    )
    assert rc.render_context(template, host="pi", inventory=inv)["port"] == 3


def test_a_role_directory_reads_the_same_defaults_as_its_template(tree):
    template, inv = tree({"own": "default"})
    assert rc.render_context(template.parents[1], inventory=inv)["own"] == "default"


def test_a_default_aliasing_an_overridden_name_carries_the_override(tree):
    # The override must be in place while values resolve, or the alias expands to the name's
    # stub before the override is laid on (#3191).
    template, inv = tree({"alias": "{{ some_secret }}"})
    ctx = rc.render_context(
        template, overrides={"some_secret": "SENTINEL"}, inventory=inv
    )
    assert ctx["alias"] == "SENTINEL"


def test_a_host_vars_mapping_replaces_the_hosts_file_and_resolves(tree):
    # A key the mapping leaves out stays out, and an alias in it expands like a file value.
    template, inv = tree(
        {"tunable": "default"},
        hosts={"box": {"tunable": "from-file", "unset_me": "x"}},
        plane="k8s",
    )
    ctx = rc.render_context(
        template, inventory=inv, host_vars={"alias": "{{ sys_user }}/a"}
    )
    assert (ctx["tunable"], ctx["alias"]) == ("default", "ubuntu/a")
    assert "unset_me" not in ctx


def test_a_value_that_will_not_expand_is_dropped(tree, capsys):
    # A dropped key renders as STUB. Kept raw, its literal braces would reach the render.
    template, inv = tree(
        {"good": "{{ sys_user }}/x", "bad": "{{ playbook_dir | no_such_filter }}"}
    )
    ctx = rc.render_context(template, inventory=inv)
    assert ctx["good"] == "ubuntu/x"
    assert "bad" not in ctx
    assert "bad left undefined" in capsys.readouterr().err


def test_a_strict_context_raises_naming_the_value_that_will_not_expand(tree):
    template, inv = tree({"bad": "{{ playbook_dir | no_such_filter }}"})
    with pytest.raises(rc.UnresolvedVarsError) as exc:
        rc.render_context(template, strict=True, inventory=inv)
    assert set(exc.value.keys) == {"bad"}


def test_a_group_var_reads_another_hosts_server_ip_through_hostvars(tree):
    # all.yml's k8s_pi_client_ip is this shape (#3719). Without the hostvars layer a strict
    # render raises on it, and every manifest that uses it fails to render.
    template, inv = tree(
        {},
        group={"pi_ip": "{{ hostvars['pi'].server_ip }}"},
        hosts={"box": {"server_ip": "10.0.0.1"}, "pi": {"server_ip": "10.0.0.2"}},
        plane="k8s",
    )
    assert (
        rc.render_context(template, strict=True, inventory=inv)["pi_ip"] == "10.0.0.2"
    )


def test_hostvars_carries_no_value_that_would_resolve_against_the_wrong_host(tree):
    # The Pi's `ansible_host: "{{ server_ip }}"` would resolve against the rendered host's
    # server_ip here, which no deploy does, so the layer holds the literal server_ip only.
    template, inv = tree(
        {},
        hosts={
            "box": {"server_ip": "10.0.0.1"},
            "pi": {"server_ip": "10.0.0.2", "ansible_host": "{{ server_ip }}"},
        },
        plane="k8s",
    )
    hostvars = rc.render_context(template, inventory=inv)["hostvars"]
    assert hostvars == {
        "box": {"server_ip": "10.0.0.1"},
        "pi": {"server_ip": "10.0.0.2"},
    }


def test_a_callers_hostvars_replaces_the_inventory_layer(tree):
    template, inv = tree({}, hosts={"box": {"server_ip": "10.0.0.1"}}, plane="k8s")
    ctx = rc.render_context(template, inventory=inv, overrides={"hostvars": {"x": {}}})
    assert ctx["hostvars"] == {"x": {}}
