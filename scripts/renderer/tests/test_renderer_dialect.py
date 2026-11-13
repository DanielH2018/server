"""The renderer holds to the closed Jinja subset ADR-0028 specifies.

The spec tests compare each construct with a literal checked once against ansible-core. The
census holds every template and variable value under the k8s roles to the subset, and a test
holds the ADR's tables equal to the literal the refusal reads.

Run: uv run pytest scripts/renderer/tests/test_renderer_dialect.py
"""

import re
from pathlib import Path

import jinja2
import pytest
from _dialect_cases import CASES, FIXTURES
from _dialect_tree import TREE_LITERAL, write_tree

from lib import yaml_fast
from lib.k8s_roles import role_dirs
from lib.render_guard import SHARED_TPL
from lib.repo_paths import ALL_VARS, HOST_VARS, K8S_ROLES, REPO
from renderer.renderer_lib.cluster import INCLUDE_KEYS, RenderError, render_tag
from renderer.renderer_lib.dialect import (
    FILTERS,
    LOOKUPS,
    TESTS,
    Construct,
    constructs,
    off_the_list,
)
from renderer.renderer_lib.environment import render_file, renderer_env
from renderer.renderer_lib.restart import TARGET_KEYS
from renderer.renderer_lib.task_inputs import include_vars

ADR = REPO / "docs" / "adr" / "0028-the-renderer-supports-a-closed-jinja-subset.md"


def _render(tmp_path: Path, template: str, variables: dict) -> str:
    (tmp_path / "case.j2").write_text(template)
    ctx = {**variables, "lookup_dir": str(FIXTURES)}
    return render_file(renderer_env(tmp_path, ctx), "case.j2", ctx)


def _tree_render(tmp_path: Path, stubs: dict | None = None, **kwargs) -> str:
    roles, inventory = write_tree(tmp_path, **kwargs)
    tag = render_tag("widget", stubs or {}, "h1", roles=roles, inventory=inventory)
    return tag.files["configmap.yaml"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.kind}-{c.name}")
def test_a_construct_renders_its_literal(tmp_path, case):
    assert _render(tmp_path, case.template, case.variables) == case.expected


def test_every_construct_on_the_list_has_a_spec_case():
    assert {(c.kind, c.name) for c in CASES} == (
        {("filter", name) for name in FILTERS}
        | {("test", name) for name in TESTS}
        | {("lookup", name) for name in LOOKUPS}
    )


def test_the_variable_layers_render_the_literal(tmp_path):
    assert _tree_render(tmp_path) == TREE_LITERAL


def test_an_extra_var_outranks_the_include(tmp_path):
    text = _tree_render(tmp_path, {"manifests_deferred_dir_name": "from-extra"})
    assert "include=from-extra\n" in text


def test_an_undefined_variable_fails_the_render_and_names_it(tmp_path):
    with pytest.raises(RenderError, match="absent"):
        _tree_render(tmp_path, template="{{ absent }}\n")


def test_another_hosts_entry_holds_no_group_var_and_fails_the_render(tmp_path):
    with pytest.raises(RenderError, match="group_only"):
        _tree_render(tmp_path, template="{{ hostvars['h2'].group_only }}\n")


def test_an_off_the_list_filter_fails_the_render_and_the_census(tmp_path):
    source = "{{ x | wordcount }}"
    assert off_the_list(source) == {Construct("filter", "wordcount")}
    with pytest.raises(jinja2.TemplateAssertionError, match="wordcount"):
        _render(tmp_path, source, {"x": "a"})


def test_an_off_the_list_test_fails_the_render_and_the_census(tmp_path):
    source = "{{ 1 is odd }}"
    assert off_the_list(source) == {Construct("test", "odd")}
    with pytest.raises(jinja2.TemplateAssertionError, match="odd"):
        _render(tmp_path, source, {})


@pytest.mark.parametrize(
    ("kind", "argument"),
    [
        ("env", "'HOME'"),
        ("pipe", "'base64 -w0 ' ~ lookup_dir ~ '/text.txt'"),
        ("ansible.builtin.template", "lookup_dir ~ '/inner.j2'"),
    ],
)
def test_an_off_the_list_lookup_fails_the_render_and_the_census(
    tmp_path, kind, argument
):
    source = f"{{{{ lookup('{kind}', {argument}) }}}}"
    assert off_the_list(source) == {Construct("lookup", kind)}
    with pytest.raises(jinja2.TemplateRuntimeError, match=re.escape(kind)):
        _render(tmp_path, source, {})


def test_a_test_passed_by_name_counts_as_a_use():
    assert Construct("test", "odd") in constructs("{{ x | select('odd') }}")
    assert Construct("test", "odd") in constructs("{{ x | selectattr('a', 'odd') }}")


def test_a_filter_passed_by_name_to_map_counts_as_a_use():
    assert off_the_list("{{ x | map('wordcount') | list }}") == {
        Construct("filter", "wordcount")
    }
    assert off_the_list("{{ x | map('upper') | list }}") == set()
    assert off_the_list("{{ x | map(attribute='a') | list }}") == set()


def test_a_construct_named_at_render_time_passed_by_name_is_off_the_list():
    assert off_the_list("{{ x | map(f) | list }}") == {Construct("filter", "<dynamic>")}
    assert off_the_list("{{ x | reject(t) | list }}") == {
        Construct("test", "<dynamic>")
    }


def test_a_lookup_named_at_render_time_is_off_the_list():
    assert off_the_list("{{ lookup(kind, 'x') }}") == {Construct("lookup", "<dynamic>")}


def _template_paths() -> list[Path]:
    return sorted(K8S_ROLES.glob("*/templates/**/*.j2")) + sorted(
        SHARED_TPL.glob("**/*.j2")
    )


def _jinja_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if "{{" in value or "{%" in value else []
    if isinstance(value, dict):
        return [s for item in value.values() for s in _jinja_values(item)]
    if isinstance(value, list):
        return [s for item in value for s in _jinja_values(item)]
    return []


def _variable_files() -> list[Path]:
    return [
        *sorted(K8S_ROLES.glob("*/defaults/*.yml")),
        ALL_VARS,
        *sorted(HOST_VARS.glob("*.yml")),
    ]


def test_every_template_stays_on_the_list():
    paths = _template_paths()
    assert len(paths) >= 300
    assert K8S_ROLES / "flux" / "templates" / "components.yaml.j2" in paths
    assert K8S_ROLES / "crowdsec" / "templates" / "verify" / "job.yaml.j2" in paths, (
        "the census no longer reaches a verify template"
    )
    offenders = {str(p): off for p in paths if (off := off_the_list(p.read_text()))}
    assert offenders == {}


def test_every_variable_value_stays_on_the_list():
    files = _variable_files()
    assert len(files) >= 50
    assert K8S_ROLES / "netpol-baseline" / "defaults" / "main.yml" in files
    offenders = {
        str(path): off
        for path in files
        for value in _jinja_values(yaml_fast.safe_load(path.read_text()))
        if (off := off_the_list(value))
    }
    assert offenders == {}


# `resolve_vars` refuses no filter, so the census is the only check on these values:
# `role_context` resolves the first set and `restart_targets` the second.
_RESOLVED_INCLUDE_KEYS = INCLUDE_KEYS | frozenset(TARGET_KEYS)


def _resolved_include_values(role: Path) -> dict:
    return {k: v for k, v in include_vars(role).items() if k in _RESOLVED_INCLUDE_KEYS}


def _include_value_problems(roles: Path) -> dict[str, set[Construct]]:
    """Each role whose render metadata hands the render a value off the list."""
    problems = {}
    for role in role_dirs(roles):
        read = _resolved_include_values(role)
        off = {c for value in _jinja_values(read) for c in off_the_list(value)}
        if off:
            problems[role.name] = off
    return problems


def test_every_include_value_the_render_resolves_stays_on_the_list():
    assert any(
        k in INCLUDE_KEYS and _jinja_values(v)
        for k, v in include_vars(K8S_ROLES / "navidrome").items()
    ), "the census no longer reaches navidrome's templated file list"
    authelia = _resolved_include_values(K8S_ROLES / "authelia")
    assert _jinja_values(authelia.get("manifests_extra_rollouts")), (
        "the census no longer reaches authelia's templated restart targets"
    )
    assert _include_value_problems(K8S_ROLES) == {}


@pytest.mark.parametrize(
    "key", ["manifests_files", "manifests_rollout", "manifests_extra_rollouts"]
)
def test_an_off_the_list_include_value_is_flagged(tmp_path, key):
    defaults = tmp_path / "widget" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text(f'{key}: "{{{{ x | wordcount }}}}"\n')
    assert _include_value_problems(tmp_path) == {
        "widget": {Construct("filter", "wordcount")}
    }


def _adr_table(heading: str) -> set[str]:
    """The backticked first cell of each row in the table under `### heading`."""
    section = ADR.read_text().split(f"### {heading}\n", 1)[1].split("\n#", 1)[0]
    return set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))


def test_the_adr_tables_name_the_literal_sets():
    assert _adr_table("Filters") == FILTERS
    assert _adr_table("Tests") == TESTS
    assert _adr_table("Lookups") == LOOKUPS
