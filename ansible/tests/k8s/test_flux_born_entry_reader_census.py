"""Every render an added Flux-born entry moves belongs to a tag its narrowing reaches (D2.1c).

`narrow_lib/containers.py:changed_fields` records an added entry that is not Ansible-owned by
the fields it carries, so the deployer applies only the list readers those fields reach. This
census renders the real tree with a fixture entry appended to daniel-box's `containers_list`,
in the role context and in `hostvars`, and compares it with a control render through the same
override and the list unchanged. The override alone moves pihole's ConfigMap and uptime-kuma's
Deployment, which is why the control is not the plain inventory render.

The comparison is by tag, never by template path. pihole's `configmap.yaml.j2` reads the list
only through `{% include 'config/pihole-dnsmasq.conf.j2' %}`, so the narrowing keeps the
included file while the render reports the including one as moved. Each moved
`(role, template)` therefore maps to its role's tag through `broad._role_tags`, and that tag
must be among the tags `broad._list_reader_tags` returns for the edit.

The narrowing reads the tree at HEAD, as `test_narrow_claim_template.py` does, while the render
reads the working tree, so commit a template edit before trusting a local run.

Run: uv run pytest ansible/tests/k8s/test_flux_born_entry_reader_census.py
"""

import functools

import pytest

from deploy_tools.narrow_lib import broad, containers
from lib.repo_paths import REPO

from _k8s_render import host_context, render_texts

BOX = "ansible/inventory/host_vars/daniel-box.yml"
_BARE = {"name": "d2-1c-fixture", "platform": "k8s", "owner": "flux"}
FIXTURES = {"bare": _BARE, "hostname": {**_BARE, "hostname": "d2-1c-fixture"}}
# The renders each fixture must move, so a census that stopped reaching them fails here: the
# list of names in `known_services.json`, observability's `blackbox-ingress` probe targets,
# and pihole's ConfigMap through `hostvars` and its include.
NAMED_MEMBERS = {
    "bare": {("artifacts", "configmap.yaml.j2")},
    "hostname": {
        ("observability", "prometheus.yaml.j2"),
        ("pihole", "configmap.yaml.j2"),
    },
}


def _docs(fixture: str | None) -> tuple[dict, dict]:
    """The host document without the fixture entry, and with it unless `fixture` is None."""
    base = host_context()["containers_list"]
    extra = [FIXTURES[fixture]] if fixture else []
    return {"containers_list": base}, {"containers_list": base + extra}


@functools.cache
def _renders(fixture: str | None) -> dict[tuple[str, str], tuple[str, ...]]:
    """Each `(role, template)`'s renders, with daniel-box's list overridden everywhere."""
    entries = _docs(fixture)[1]["containers_list"]
    hostvars = host_context()["hostvars"]
    box = {**hostvars.get("daniel-box", {}), "containers_list": entries}
    overrides = {
        "containers_list": entries,
        "hostvars": {**hostvars, "daniel-box": box},
    }
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for role, template, text in render_texts(overrides):
        found[(role, template)] = found.get((role, template), ()) + (text,)
    return found


def _moved(fixture: str) -> set[tuple[str, str]]:
    control, edited = _renders(None), _renders(fixture)
    return {
        key
        for key in control.keys() | edited.keys()
        if control.get(key) != edited.get(key)
    }


@functools.cache
def _ctx() -> broad.Context:
    return broad.context_for("HEAD", REPO)


def _unreached(fixture: str, edit: containers.Edit) -> dict[str, list[str]]:
    """Each tag a moved render maps to that `_list_reader_tags` leaves out, with its renders."""
    ctx = _ctx()
    reached = broad._list_reader_tags(ctx, BOX, edit)
    missing: dict[str, list[str]] = {}
    for role, template in sorted(_moved(fixture)):
        for tag in sorted(broad._role_tags({role}, ctx) - reached):
            missing.setdefault(tag, []).append(f"{role}/{template}")
    return missing


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_every_render_a_flux_born_entry_moves_is_a_tag_it_reaches_is_clean(
    fixture: str,
):
    moved = _moved(fixture)
    assert NAMED_MEMBERS[fixture] <= moved, (
        f"the {fixture} fixture no longer moves {sorted(NAMED_MEMBERS[fixture] - moved)}; "
        "the census lost a member it must find"
    )
    edit = containers.edit(*_docs(fixture), BOX)
    assert _unreached(fixture, edit) == {}, (
        "these tags' renders move for an added Flux-born entry, but its narrowing "
        "applies none of them"
    )


def test_an_own_tag_only_narrowing_misses_artifacts_is_flagged():
    """The red half: a `changed` holding no entry reaches no reader, so artifacts is missed."""
    after = _docs("bare")[1]
    edit = containers.Edit({}, containers.job_owners(after), "daniel-box")
    assert "artifacts" in _unreached("bare", edit)
