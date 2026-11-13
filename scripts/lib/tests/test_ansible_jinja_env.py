"""Tests for lib.ansible_jinja_env — the one environment every render guard builds on.

Run: uv run pytest scripts/lib/tests/test_ansible_jinja_env.py
"""

import re
import sys

import pytest

from lib import ansible_jinja_env as aje
from lib.k8s_yaml import to_json_stub
from lib.repo_paths import REPO, SHARED_TPL
from lib.proc_testing import run

# Every filter and test a template in this tree reaches for, mapped to the implementation it
# must resolve to. A roster rather than a count, so a filter that silently stops being
# registered fails by name.
EXPECTED_FILTERS = {
    "bool": "lib.ansible_jinja_env.ansible_bool",
    "comment": "renderer.renderer_lib.jinja_filters.comment",
    "hash": "renderer.renderer_lib.jinja_filters.hash_text",
    "mandatory": "renderer.renderer_lib.jinja_filters.mandatory",
    "to_uuid": "renderer.renderer_lib.jinja_filters.to_uuid",
    "filter_by_platform": "toposort.filter_by_platform",
    "authelia_service_rules": "authelia_access.authelia_service_rules",
    "lan_hostnames": "entry_fields.lan_hostnames",
    "netpol_caller_fences": "homepage_tiles.netpol_caller_fences",
}


@pytest.mark.parametrize("name,dotted", sorted(EXPECTED_FILTERS.items()))
def test_every_registered_filter_is_the_implementation_on_the_roster(
    name: str, dotted: str
):
    """A guard and the renderer read one set, which `test_renderer_filters.py` holds to Ansible's results."""
    env = aje.make_ansible_env([SHARED_TPL])
    registered = env.filters[name]
    assert f"{registered.__module__}.{registered.__name__}" == dotted


def test_to_json_is_the_stub_that_tolerates_an_undefined_value():
    """A guard renders every SOPS secret as StubUndefined, which plain-data JSON refuses.

    That would abort the render this environment exists to complete.
    """
    env = aje.make_ansible_env([SHARED_TPL])
    assert env.filters["to_json"] is to_json_stub
    assert env.from_string("{{ missing | to_json }}").render() == '"STUB"'


def test_the_search_test_is_registered_and_searches_rather_than_matches():
    """Ansible's `search` is a regex SEARCH; vanilla Jinja2 has no `search` test at all.

    The RED half is the second assertion: a `match`-flavoured implementation anchors at the
    start and would answer False for a pattern found mid-string.
    """
    env = aje.make_ansible_env([SHARED_TPL])
    assert env.from_string("{{ '2400:cb00::/32' is search(':') }}").render() == "True"
    assert env.from_string("{{ '172.64.0.0/13' is search(':') }}").render() == "False"


def test_bool_renders_the_string_false_the_way_a_deploy_does():
    """`-e var=false` arrives as the STRING "false", which plain Jinja truthiness reads True.

    Rendering it the Python way is the failure this whole registration exists to prevent, so
    assert the rendered branch rather than the filter's identity.
    """
    env = aje.make_ansible_env([SHARED_TPL])
    assert (
        env.from_string("{% if 'false' | bool %}yes{% else %}no{% endif %}").render()
        == "no"
    )


def test_template_env_loads_the_role_dir_and_the_shared_dir(tmp_path):
    """Every guard loads that pair — a role's templates and the macros they import."""
    (tmp_path / "own.j2").write_text("own\n")
    env = aje.template_env(tmp_path)
    assert env.get_template("own.j2").render() == "own\n"
    shared = next(SHARED_TPL.glob("*.j2"))
    assert env.get_template(shared.name) is not None


def test_render_template_raises_on_a_template_that_will_not_render(tmp_path):
    tpl = tmp_path / "broken.j2"
    tpl.write_text("{% if %}\n")
    with pytest.raises(RuntimeError, match="render error"):
        aje.render_template(tpl, {})


def test_render_template_stubs_an_undefined_variable(tmp_path):
    tpl = tmp_path / "sample.j2"
    tpl.write_text("value={{ some_never_defined_var }}\n")
    assert aje.render_template(tpl, {}) == "value=STUB\n"


# `to_bool`'s result for the inputs its tables name, the inputs `convert_bool.boolean`
# accepts and `to_bool` does not, and the fallback and unhashable shapes outside both, as
# ansible-core 2.22 returned them before H7 removed it.
_TO_BOOL = [
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("true", True),
    ("false", False),
    ("TRUE", True),
    ("Off", False),
    ("yes", True),
    ("no", False),
    ("on", True),
    ("off", False),
    ("1", True),
    ("0", False),
    ("", False),
    ("maybe", False),
    ("t", False),
    ("y", False),
    ("f", False),
    ("n", False),
    (2, False),
    (-1, False),
    (" True ", False),
    (1.0, True),
    (2.0, False),
    (0.0, False),
    (None, False),
    ([], False),
    ({}, False),
]


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
        # `to_bool` returns False for anything outside its tables rather than raising — the
        # branch that matters, since plain Jinja would call a non-empty string True.
        ("maybe", False),
        # The rejecting half: `convert_bool.boolean` accepts these, the `bool` FILTER does
        # not, and the shim must mirror the filter.
        ("t", False),
        ("y", False),
        (2, False),
        (-1, False),
        (" True ", False),
    ],
)
def test_ansible_bool_filter_mirrors_ansible_semantics(value, expected: bool):
    assert aje.ansible_bool(value) is expected


@pytest.mark.parametrize(("value", "expected"), _TO_BOOL, ids=repr)
def test_ansible_bool_returns_what_ansible_core_to_bool_returned(value, expected: bool):
    """Includes the `== 1` fallback, which ansible-core 2.23 drops from its own filter."""
    assert aje.ansible_bool(value) is expected


def test_the_light_tier_loads_no_ansible_core():
    """`probe_lib/health.py` imports `k8s_context`, so importing these stays cheap.

    A subprocess rather than `sys.modules` in-process, because pytest has already imported
    ansible-core by the time this test runs.
    """
    proc = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); import lib.k8s_context, lib.ansible_jinja_env, lib.render_guard, lib.k8s_yaml; print([m for m in sys.modules if m.startswith('ansible')])",
        ],
        cwd=REPO,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", (
        f"the light tier now imports ansible-core\n{proc.stdout}"
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        (
            "ingress_probe_targets",
            ([{"name": "svc", "hostname": "svc", "probe": "yes"}],),
        ),
        ("backup_group_label", ("no-such-group",)),
        ("weekly_backup_group", (9,)),
        ("weekly_backup_shard", ("ns/absent", {})),
        ("longhorn_backup_name", ("no_such_key",)),
    ],
)
def test_a_guard_refusal_names_its_filter_as_a_deploy_does(name: str, args: tuple):
    """The guard env registers the FilterModule versions, so its refusal reads as a deploy's (#4505)."""
    from filter_errors import FilterError

    env = aje.make_ansible_env([SHARED_TPL])
    with pytest.raises(FilterError) as caught:
        env.filters[name](*args)
    assert str(caught.value).startswith(f"{name}: "), str(caught.value)


def test_template_env_reads_a_file_lookup_and_refuses_any_other_plugin(tmp_path):
    """A setup template inlines a file with `lookup('file', ...)`; no other plugin has a stand-in."""
    data = tmp_path / "data.txt"
    data.write_text("line\n\n")
    (tmp_path / "inline.j2").write_text("{{ lookup('file', path) }}|\n")
    (tmp_path / "other.j2").write_text("{{ lookup('pipe', 'date') }}\n")
    env = aje.template_env(tmp_path)
    assert env.get_template("inline.j2").render(path=str(data)) == "line|\n"
    with pytest.raises(ValueError, match="no stand-in"):
        env.get_template("other.j2").render()


# `-e var=...`, `--extra-vars` or "extra vars": a playbook's command-line variables, which no
# render path at HEAD has (#8182).
_EXTRA_VARS = re.compile(r"(?<![\w-])-e\s+\w+=|extra[- _]vars", re.IGNORECASE)


@pytest.mark.parametrize("name", ["ansible_bool", "register_ansible_filters"])
def test_bool_rationale_cites_no_extra_vars_path(name: str):
    doc = getattr(aje, name).__doc__ or ""
    assert not _EXTRA_VARS.search(doc), _EXTRA_VARS.findall(doc)
