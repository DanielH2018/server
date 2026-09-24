"""Tests for lib.ansible_jinja_env — the one environment every render guard builds on.

Run: uv run pytest scripts/lib/tests/test_ansible_jinja_env.py
"""

import subprocess
import sys

import pytest

from lib import ansible_jinja_env as aje
from lib.k8s_yaml import to_json_stub
from lib.repo_paths import REPO, SHARED_TPL

# Every filter and test a template in this tree reaches for, mapped to the implementation it
# must resolve to. A roster rather than a count, so a filter that silently stops being
# registered fails by name. `to_json` is the one entry that is not ansible-core's: the real
# one raises on the StubUndefined a guard renders secrets as.
EXPECTED_FILTERS = {
    "bool": "ansible.plugins.filter.core.to_bool",
    "comment": "ansible.plugins.filter.core.comment",
    "hash": "ansible.plugins.filter.core.get_hash",
    "mandatory": "ansible.plugins.filter.core.mandatory",
    "to_uuid": "ansible.plugins.filter.core.to_uuid",
    "filter_by_platform": "toposort.filter_by_platform",
    "authelia_service_rules": "authelia_access.authelia_service_rules",
}


@pytest.mark.parametrize("name,dotted", sorted(EXPECTED_FILTERS.items()))
def test_every_registered_filter_is_the_real_implementation(name: str, dotted: str):
    """The point of the module: a guard agrees with a deploy by identity, not by a shim.

    A hand-written stub agrees with the real filter on the ordinary inputs a test reaches for
    and diverges on exactly the ones the filter exists for — `bool("false")` being the case
    that started this (#2074, #2408).
    """
    env = aje.make_ansible_env([SHARED_TPL])
    registered = env.filters[name]
    assert f"{registered.__module__}.{registered.__name__}" == dotted


def test_to_json_is_the_stub_that_tolerates_an_undefined_value():
    """The one filter that is deliberately not ansible-core's.

    A guard renders every SOPS secret as StubUndefined, which ansible-core's `to_json` refuses
    to serialize — it would abort the render this environment exists to complete.
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


def test_the_light_tier_loads_no_ansible_core():
    """`lib.k8s_context`'s `DECIDED:` marker costs ~190 ms if this module leaks into it.

    `probe_lib/monitors.py` imports `k8s_context`, and `probe.py monitors` loads no
    ansible-core today. A subprocess rather than `sys.modules` in-process, because pytest has
    already imported ansible-core by the time this test runs.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts');"
            " import lib.k8s_context, lib.render_guard, lib.k8s_yaml;"
            " print([m for m in sys.modules if m.startswith('ansible')])",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", (
        "the light tier now imports ansible-core — see the DECIDED: marker in "
        f"scripts/lib/k8s_context.py\n{proc.stdout}"
    )
