"""The PriorityClass manifest must not state a field the API server drops.

`kubectl apply` decides "configured" vs "unchanged" by a three-way merge across the manifest,
kubectl's `last-applied-configuration` annotation, and the live object. A field whose Go
struct tag is `omitempty` disappears from the live object when it holds its zero value — so
writing it explicitly puts it in two of those three places and never the third. The merge
finds a patch every time and the apply never converges.

`globalDefault: false` on a PriorityClass is exactly that field, and it read that way here
until 2026-08-27. Measured on daniel-stage: two consecutive applies of the old file both
printed "configured" for all four classes; with the field removed the first converges and the
second prints "unchanged".

It is worth a test rather than a comment because the field is the kind a reviewer ADDS. It
looks like explicitness — stating the default so nobody has to look it up — and the cost is
invisible unless you run the same play twice and read the changed count.

This checks the shape, not the rendering: the template has no Jinja in the affected lines, so
a textual guard is the whole check rather than an approximation of it.
"""

import pytest
import yaml

from _helpers import ROLES

TEMPLATE = ROLES / "setup" / "k3s" / "templates" / "priorityclass.yaml.j2"

# Fields the API server omits from a PriorityClass when they hold their zero value. Each is
# safe to state and expensive to state. Extend this as others are found — the point is the
# class, not the one instance.
DEFAULTED_AWAY = ("globalDefault",)


def _documents():
    docs = [d for d in yaml.safe_load_all(TEMPLATE.read_text()) if d]
    assert docs, (
        f"{TEMPLATE} parsed to no documents — check the loader, not the manifest."
    )
    return docs


def test_the_manifest_defines_the_four_tiers():
    """Guards the derivation: an empty parse would make the check below vacuous."""
    names = [d["metadata"]["name"] for d in _documents()]
    assert len(names) == 4, f"expected 4 PriorityClasses in {TEMPLATE}, found {names}."
    assert len(set(names)) == 4, (
        f"duplicate PriorityClass names in {TEMPLATE}: {names}."
    )


@pytest.mark.parametrize("field", DEFAULTED_AWAY)
def test_no_document_states_a_field_the_server_drops(field):
    offenders = [d["metadata"]["name"] for d in _documents() if field in d]
    assert not offenders, (
        f"{offenders} state {field!r} in {TEMPLATE}. The API server drops it at its zero "
        f"value, so it lands in the manifest and in kubectl's last-applied annotation but "
        f"never in the live object — and every `kubectl apply` of this file then reports "
        f"'configured' forever. Stating the default buys nothing: omitting it produces the "
        f"same scheduling behaviour and lets the apply converge."
    )


def test_every_document_still_carries_a_value():
    """The field that actually matters is still there — this is not a licence to strip more."""
    for doc in _documents():
        assert isinstance(doc.get("value"), int), (
            f"PriorityClass {doc['metadata']['name']} in {TEMPLATE} has no integer `value`. "
            f"That one is load-bearing: it is the priority itself, and it is not defaulted."
        )
