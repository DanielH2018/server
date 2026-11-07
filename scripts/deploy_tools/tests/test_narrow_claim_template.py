"""The two importer edges `narrow_templates` corrects: a comment, and the claim template (#3445).

Each rule is a pair over the `_narrow_fixtures` tree: the importers it narrows to, and the tree
it must still refuse or widen. The two real-tree tests pin the facts the claim rule rests on, so a
`manifests` task that renders the template outside `k8s_claims` fails here rather than leaving
its callers stale.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_claim_template.py
"""

import pytest


from deploy_tools.deploy_lib import tags as deploy_tags
from deploy_tools.narrow_lib import role_reads
from deploy_tools.narrow_lib import templates as narrow_templates
from lib.git import git_stdout
from lib.narrow_git import CannotNarrow
from lib.repo_paths import REPO

from _narrow_fixtures import Tree, build_tree

CLAIM_NAME = "claim-default.yaml.j2"
CLAIM = f"ansible/templates/{CLAIM_NAME}"
MACRO_NAME = "container-resources.yml.j2"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
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


def _importers(tree: Tree, name: str) -> set[str]:
    return role_reads.template_importers(name, "HEAD", tree.root, {name})


def test_the_claim_template_reaches_the_roles_declaring_k8s_claims(tree: Tree):
    assert _importers(tree, CLAIM_NAME) == {"sonarr"}


def test_k8s_claims_set_through_include_vars_is_flagged(tree: Tree):
    """A key set outside a role's defaults reaches a role the declarer scan cannot name."""
    tree.write(
        "ansible/roles/k8s/radarr/tasks/main.yml",
        "- ansible.builtin.include_role:\n    name: k8s/volume-revert\n"
        "  vars:\n    k8s_claims: [{name: r, size: 1Gi, storage_class: longhorn}]\n",
    )
    tree.commit("radarr sets the key at its include")
    with pytest.raises(CannotNarrow, match=r"radarr/tasks/main\.yml"):
        _importers(tree, CLAIM_NAME)


def test_a_macro_named_only_in_a_jinja_comment_is_clean(tree: Tree):
    tree.write(
        "ansible/roles/k8s/radarr/templates/deployment.yaml.j2",
        "{# sizes come from container-resources.yml.j2 elsewhere #}\na: b\n",
    )
    tree.commit("radarr mentions the macro in a comment")
    assert _importers(tree, MACRO_NAME) == {"sonarr"}


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
    assert _importers(tree, MACRO_NAME) == {"sonarr", "radarr", "bazarr"}


def _declaring_roles() -> set[str]:
    """The roles whose defaults declare `k8s_claims` at HEAD, the ref the rule reads."""
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


def test_the_real_claim_template_reaches_exactly_its_declarers():
    declarers = _declaring_roles()
    assert {
        "freshrss",
        "livesync",
        "speedtest",
        "zigbee2mqtt",
    } <= declarers
    ctx = role_reads.context_for("HEAD", REPO)
    roles = role_reads.template_importers(CLAIM_NAME, "HEAD", REPO, {CLAIM_NAME})
    assert role_reads.role_tags(roles, ctx) == declarers


_MACRO_BEFORE = (
    "{# sizes for every container #}\n"
    "{% macro resources(c) %}{{ c.cpu }}{% endmacro %}\n"
    'x: {{ "{# literal #}" }}\n'
)


def test_an_edit_inside_a_jinja_comment_renders_the_same():
    """#3459: an edit inside `{# #}`, even one adding lines, renders no bytes."""
    edited = _MACRO_BEFORE.replace("container #}", "container,\n   on two lines #}")
    assert narrow_templates.comment_only(_MACRO_BEFORE, edited)


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
def test_an_edit_that_renders_differently_is_not_comment_only(edited: str):
    assert not narrow_templates.comment_only(_MACRO_BEFORE, edited)


# -- a Jinja-comment-only shared-template edit is quiet (#4926) ---------------------------
#
# PR #4925 edited only the `{# ... #}` doc block of a shared template, and the landing named a
# full `ansible/deploy.yml` for it. In a `.j2` a `#` line is rendered content, so the RED twin
# edits one of those and must stay loud. No tick applies `ansible/deploy.yml` since D6.12, so
# the quiet set is what is left to pin.

_SHARED_TEMPLATE = "ansible/templates/checksum-annotation.yml.j2"
_TEMPLATE_BEFORE = "{# Doc block. #}\n# rendered: yes\nchecksum: {{ x }}\n"


@pytest.mark.parametrize(
    "after, quiet",
    [
        (
            "{# Doc block,\n   reworded. #}\n# rendered: yes\nchecksum: {{ x }}\n",
            {_SHARED_TEMPLATE},
        ),
        ("{# Doc block. #}\n# rendered: no\nchecksum: {{ x }}\n", set()),
    ],
    ids=["jinja-comment-only", "yaml-hash-line-is-content"],
)
def test_a_jinja_comment_only_template_edit_is_quiet(after, quiet):
    texts = {
        ("old", _SHARED_TEMPLATE): _TEMPLATE_BEFORE,
        ("new", _SHARED_TEMPLATE): after,
    }
    found = deploy_tags.comment_only_paths(
        [_SHARED_TEMPLATE], "old", "new", lambda ref, path: texts[(ref, path)]
    )
    assert found == quiet
