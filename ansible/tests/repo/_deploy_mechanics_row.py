"""The census row that no template reads a deploy-mechanics field, such as `owner`.

Target slice D2's PR D2.1a (docs/target-architecture/deploy/d2-first-leaf.md).
`narrow_lib/containers.py:changed_fields` leaves `DEPLOY_MECHANICS_FIELDS` out of a
`containers_list` entry edit, and this row keeps that sound. It sits in its own module because
`test_census_rows_roles.py`, which imports it into `ROWS`, is near the cap for a test module.

The predicate reads Jinja code only, inside `{{ }}` or `{% %}` with `{# #}` stripped. When the
row landed, 13 template files used the word `owner` in YAML comments or JSON, and none inside
a Jinja expression.
"""

import re

from _row_table import Census, Subject, tracked
from deploy_tools.narrow_lib.containers import DEPLOY_MECHANICS_FIELDS
from lib.jinja_comments import strip_jinja_comments

_JINJA_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# A member as a whole word. Inside Jinja code `owner-1` is a subtraction, so `-` ends a word.
# An empty set matches nothing, so losing `owner` from it fails the red `entry.owner` subject.
_MECHANICS_READ = re.compile(
    r"(?<!\w)(" + ("|".join(sorted(DEPLOY_MECHANICS_FIELDS)) or r"(?!)") + r")(?!\w)"
)


def _mechanics_reads(subject: Subject) -> list[str]:
    """Every deploy-mechanics field a Jinja expression in `subject` names, by line."""
    text = strip_jinja_comments(subject.text)
    return [
        f"line {text.count(chr(10), 0, code.start()) + 1} reads `{read.group(1)}`"
        for code in _JINJA_CODE.finditer(text)
        for read in _MECHANICS_READ.finditer(code.group(0))
    ]


DEPLOY_MECHANICS = Census(
    name="no-template-reads-a-deploy-mechanics-field",
    reason=(
        "`narrow_lib/containers.py:changed_fields` leaves `DEPLOY_MECHANICS_FIELDS` out of "
        "an entry edit, so an edit to `owner` alone reaches no template reader and deploys "
        "the entry's own tag only (D2.1a, docs/target-architecture/deploy/d2-first-leaf.md). "
        "A template that read one would render differently behind a narrowing that never "
        "applied it. Read the field in a script or a filter plugin, never in a template. "
        "Only Jinja code counts: a YAML comment, a JSON key or a `{# #}` naming `owner` is "
        "prose."
    ),
    files=lambda: tracked("ansible/roles/k8s/*/templates/*", "ansible/templates/*"),
    offence=_mechanics_reads,
    red=(
        Subject("deployment.yaml.j2", "{% if entry.owner == 'flux' %}x{% endif %}\n"),
        Subject("configmap.yaml.j2", "a: {{ entry['owner'] }}\n"),
        Subject("service.yaml.j2", "{{ x | selectattr('owner', 'defined') }}\n"),
    ),
    green=(
        Subject("a.yaml.j2", "# owner: the role that applies this\n"),
        Subject("b.json.j2", '{"owner": "{{ name }}"}\n'),
        Subject("c.yaml.j2", "{# entry.owner is read by scripts only #}\n"),
        Subject("d.yaml.j2", "{{ owner_ref }} {{ x.owners }}\n"),
    ),
    # 308 templates when the row landed (2026-10-14).
    min_matches=250,
    must_find=frozenset(
        {
            "ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2",
            "ansible/templates/service-default.yaml.j2",
            "ansible/templates/ingressroute-default.yaml.j2",
        }
    ),
)
