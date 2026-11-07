"""Guards for the prune label an armed role renders.

An armed role renders through `ansible/templates/role-labelled.yaml.j2`, whose
`homelab_role_label` filter stamps the label onto every document (#3388). Flux's own inventory
decides what it prunes, so the label names the role on each live object.

Run: uv run pytest ansible/tests/deploy/test_manifests_prune.py
"""

import pytest
from _helpers import (
    ROLES,
)
from renderer.renderer_lib.task_inputs import render_defaults
from _k8s_render import render_role_template, rendered_texts
from lib import yaml_fast
from lib.k8s_roles import (
    MANIFESTS_PRUNE_DEFAULT,
    tag_owners,
)
from role_label import ROLE_LABEL, homelab_role_label


# The roles that opt out of the prune, each by operator decision recorded as a DECIDED marker
# in its defaults (#3388). Every other role is armed.
_UNARMED_ROLES = frozenset({"authelia", "nut"})


def _armed_roles() -> dict[str, dict]:
    """Each armed role and its render defaults; a role omitting the flag takes the default."""
    armed = {}
    for tag in tag_owners():
        defaults = render_defaults(ROLES / "k8s" / tag)
        if defaults.get("manifests_prune", MANIFESTS_PRUNE_DEFAULT) is True:
            armed[tag] = defaults
    return armed


_ARMED_ROLES = frozenset(_armed_roles())


def test_every_caller_arms_the_prune_but_the_decided_exclusions() -> None:
    """registry, the first armed role, and observability, armed in its own namespace, are the
    named members: a census that stopped finding the tags would pass on an empty set."""
    callers = set(tag_owners())
    assert {"registry", "observability"} <= _ARMED_ROLES
    assert callers - _ARMED_ROLES == _UNARMED_ROLES, (
        f"unarmed callers {sorted(callers - _ARMED_ROLES)} differ from the decided exclusions "
        f"{sorted(_UNARMED_ROLES)}. A role is armed unless it sets `manifests_prune: false`; "
        "excluding one needs an operator decision and a DECIDED marker in its defaults."
    )


def test_an_empty_render_is_flagged() -> None:
    with pytest.raises(ValueError, match="rendered no document"):
        homelab_role_label("# nothing rendered\n---\n", "widget")


def test_a_rendered_object_is_clean() -> None:
    out = homelab_role_label(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: w\n", "widget"
    )
    assert yaml_fast.safe_load(out)["metadata"]["labels"] == {ROLE_LABEL: "widget"}


def test_another_roles_label_is_flagged() -> None:
    held = f"kind: Service\nmetadata:\n  name: w\n  labels:\n    {ROLE_LABEL}: gadget\n"
    with pytest.raises(ValueError, match="already carries"):
        homelab_role_label(held, "widget")


def _env_value_line(value: str) -> str:
    text = homelab_role_label(
        f"kind: ConfigMap\nmetadata:\n  name: w\ndata:\n  v: {value}\n", "widget"
    )
    return next(line for line in text.splitlines() if line.startswith("  v:"))


@pytest.mark.parametrize("value", ['"y"', '"N"', '"1e3"', '"0o17"', '"0b1"', '"on"'])
def test_a_string_kubectl_reads_as_a_number_or_bool_is_written_quoted(
    value: str,
) -> None:
    """kubectl's go-yaml v2 resolves these plain scalars to bools and numbers; PyYAML does not."""
    assert _env_value_line(value) == f"  v: {value}"


@pytest.mark.parametrize("value", ["0o444", "y", "1e3"])
def test_a_plain_scalar_kubectl_reads_as_a_number_or_bool_stays_plain(
    value: str,
) -> None:
    """scrutiny's `defaultMode: 0o444` is a string to PyYAML and 292 to go-yaml.

    Quoted, the API server receives the string "0o444" and refuses the Deployment.
    """
    assert _env_value_line(value) == f"  v: {value}"


def test_an_ordinary_string_and_the_keys_stay_plain() -> None:
    assert _env_value_line("plain-text") == "  v: plain-text"
    text = homelab_role_label("kind: Service\nmetadata:\n  name: w\n", "widget")
    assert "kind: Service" in text.splitlines()


def _without_role_label(doc: dict) -> dict:
    labels = dict(doc["metadata"]["labels"])
    labels.pop(ROLE_LABEL)
    metadata = {**doc["metadata"], "labels": labels}
    if not labels:
        metadata.pop("labels")
    return {**doc, "metadata": metadata}


def test_every_armed_render_carries_the_label() -> None:
    """The selector filters the apply, so an unlabelled document would stop being applied.

    bazarr's claim and littlelink's shared Service are the named members.
    """
    armed = _armed_roles()
    seen = set()
    for role, name, text in rendered_texts():
        deferred = armed.get(role, {}).get("manifests_deferred_files") or []
        if role not in armed or name.removesuffix(".j2") in deferred:
            continue
        seen.add((role, name))
        for doc in yaml_fast.safe_load_all(text):
            if doc:
                assert doc["metadata"]["labels"][ROLE_LABEL] == role, f"{role}/{name}"
    assert {
        ("bazarr", "claim-default.yaml.j2"),
        ("littlelink", "service-default.yaml.j2"),
    } <= seen


def test_the_filter_adds_the_label_and_nothing_else() -> None:
    """The armed roles' real unlabelled renders, passed through the filter the wrapper calls.

    Each document gains the label and nothing else: a Deployment's selector and pod-template
    labels, a quoted string and a multi-line value all read back as rendered, and the host's
    stdlib reader declares the same objects from the new bytes. A claim renders per entry, so
    it is out of this walk, and `test_every_armed_render_carries_the_label` holds it.
    """
    armed = _armed_roles()
    seen = set()
    for role, name, _ in rendered_texts():
        deferred = armed.get(role, {}).get("manifests_deferred_files") or []
        if (
            role not in armed
            or name == "claim-default.yaml.j2"
            or name.removesuffix(".j2") in deferred
        ):
            continue
        seen.add((role, name))
        text = render_role_template(role, name)
        before = [d for d in yaml_fast.safe_load_all(text) if d is not None]
        labelled = homelab_role_label(text, role)
        after = list(yaml_fast.safe_load_all(labelled))
        assert len(after) == len(before), f"{role}/{name} gained or lost a document"
        for old, new in zip(before, after, strict=True):
            assert new["metadata"]["labels"][ROLE_LABEL] == role, f"{role}/{name}"
            assert _without_role_label(new) == old, (
                f"{role}/{name} changed beyond the label"
            )
    assert ("littlelink", "service-default.yaml.j2") in seen
