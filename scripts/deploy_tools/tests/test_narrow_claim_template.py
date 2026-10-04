"""The two importer edges `narrow_templates` corrects: a comment, and the claim template (#3445).

Each rule is a pair over the `_narrow_fixtures` tree: the range it narrows, and the range it
must still refuse or widen. The two real-tree tests pin the facts the claim rule rests on, so a
`manifests` task that renders the template outside `k8s_claims` fails here rather than leaving
its callers stale.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_claim_template.py
"""

import pytest


import narrow_broad
from lib import yaml_fast
from lib.git import git_stdout
from lib.repo_paths import REPO

from _narrow_fixtures import Tree, _refs, build_tree

CLAIM = "ansible/templates/claim-default.yaml.j2"
RENDERER = "ansible/roles/k8s/manifests/tasks/main.yml"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    # `manifests` is a shared role with no tag and, in this fixture, no caller: without the
    # claim rule a hit in it refuses, so a green narrowing below is the rule's own work.
    t.write(
        RENDERER,
        "- ansible.builtin.template:\n"
        "    src: '{{ playbook_dir }}/templates/claim-default.yaml.j2'\n"
        '  loop: "{{ k8s_claims | default([]) }}"\n',
    )
    t.write(CLAIM, "{{ manifests_claim.name }}\n")
    t.write(
        "ansible/roles/k8s/sonarr/defaults/main.yml",
        "k8s_claims:\n  - {name: sonarr-data, size: 1Gi, storage_class: longhorn}\n",
    )
    t.write(
        "ansible/roles/k8s/radarr/tasks/main.yml", "# k8s_claims lives in defaults\n"
    )
    t.commit("claims")
    return t


def test_a_claim_template_change_narrows_to_the_roles_declaring_k8s_claims(tree: Tree):
    tree.write(CLAIM, "{{ manifests_claim.size }}\n")
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_k8s_claims_set_through_include_vars_is_flagged(tree: Tree):
    """A key set outside a role's defaults reaches a role the declarer scan cannot name."""
    tree.write(
        "ansible/roles/k8s/radarr/tasks/main.yml",
        "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
        "  vars:\n    k8s_claims: [{name: r, size: 1Gi, storage_class: longhorn}]\n",
    )
    tree.commit("radarr sets the key at its include")
    tree.write(CLAIM, "{{ manifests_claim.size }}\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="radarr/tasks/main.yml"):
        tree.narrow(*_refs(tree))


def test_a_macro_named_only_in_a_jinja_comment_is_clean(tree: Tree):
    tree.write(
        "ansible/roles/k8s/radarr/templates/deployment.yaml.j2",
        "{# sizes come from container-resources.yml.j2 elsewhere #}\na: b\n",
    )
    tree.commit("radarr mentions the macro in a comment")
    tree.write(
        "ansible/templates/container-resources.yml.j2", "{% macro resources(c) %}\n"
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_macro_imported_beside_a_comment_naming_it_is_flagged(tree: Tree):
    """The pair: a mention outside the comment still counts, and so does one in a raw block."""
    tree.write(
        "ansible/roles/k8s/radarr/templates/deployment.yaml.j2",
        "{# see container-resources.yml.j2 #}\n"
        "{% from 'container-resources.yml.j2' import resources %}\n",
    )
    tree.write(
        "ansible/roles/k8s/bazarr/templates/deployment.yaml.j2",
        "{% raw %}{# {% endraw %}{# container-resources.yml.j2 #}\n",
    )
    tree.commit("radarr imports the macro, bazarr carries a raw block")
    tree.write(
        "ansible/templates/container-resources.yml.j2", "{% macro resources(c) %}\n"
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "radarr", "bazarr"}


def _declaring_roles() -> set[str]:
    """The roles whose defaults declare `k8s_claims` at HEAD, the ref the narrowing reads."""
    out = git_stdout(
        "grep",
        "-l",
        "-E",
        "^k8s_claims:",
        "HEAD",
        "--",
        "ansible/roles/*/*/defaults/main.yml",
        cwd=REPO,
    )
    return {line.split("/")[-3] for line in out.splitlines()}


def test_the_real_claim_template_narrows_to_exactly_its_declarers():
    declarers = _declaring_roles()
    assert {
        "freshrss",
        "livesync",
        "speedtest",
        "zigbee2mqtt",
    } <= declarers
    ctx = narrow_broad.context_for("HEAD", REPO)
    assert narrow_broad.broad_path_tags(CLAIM, "HEAD", ctx) == declarers


def test_manifests_renders_the_claim_template_only_per_k8s_claims_entry():
    """The fact the claim rule rests on: one task names the template, and it loops the key."""
    tasks = yaml_fast.safe_load((REPO / RENDERER).read_text())
    naming = [t for t in tasks if "claim-default.yaml.j2" in str(t)]
    assert len(naming) == 1
    assert "k8s_claims" in naming[0]["loop"]


MACRO = "ansible/templates/container-resources.yml.j2"


@pytest.fixture
def importer(tree: Tree) -> Tree:
    """`sonarr` imports the macro, with a comment and a string holding `{#` beside the code."""
    tree.write(
        MACRO,
        "{# sizes for every container #}\n"
        "{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\n"
        'x: {{ "{# literal #}" }}\n',
    )
    tree.write(
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "{% from 'container-resources.yml.j2' import resources %}\n",
    )
    tree.commit("sonarr imports the macro")
    return tree


def test_a_comment_only_template_edit_narrows_to_nothing(importer: Tree):
    """#3459: an edit inside `{# #}`, even one adding lines, renders no bytes."""
    importer.write(
        MACRO,
        "{# sizes for every container,\n   now on two lines #}\n"
        "{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\n"
        'x: {{ "{# literal #}" }}\n',
    )
    assert importer.narrow(*_refs(importer)) == set()


@pytest.mark.parametrize(
    "edited",
    [
        # a comment edit beside a code edit
        "{# sizes #}\n{% macro resources(c) %}{{ c.mem }}{% endmacro %}\n"
        'x: {{ "{# literal #}" }}\n',
        # whitespace control added to the comment strips the data around it
        "{# sizes for every container -#}\n"
        "{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\n"
        'x: {{ "{# literal #}" }}\n',
        # a `{#` inside a string literal is content, not a comment
        "{# sizes for every container #}\n"
        "{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\n"
        'x: {{ "{# edited #}" }}\n',
        # a whole comment line removed changes the render without trim_blocks
        '{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\nx: {{ "{# literal #}" }}\n',
    ],
    ids=["code-too", "whitespace-control", "string-literal", "comment-line-removed"],
)
def test_an_edit_that_renders_differently_still_reaches_its_importers(
    importer: Tree, edited: str
):
    importer.write(MACRO, edited)
    assert importer.narrow(*_refs(importer)) == {"sonarr"}
