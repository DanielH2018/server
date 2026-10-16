"""The prune exemption: `kustomize.toolkit.fluxcd.io/prune: disabled` on claims and Namespaces (D5.1a2).

kustomize-controller garbage-collects an object it applied once the object leaves the render.
A PersistentVolumeClaim or a Namespace dropped that way takes its data, or every object in it,
with it. C3 *Prune exemptions* therefore has the renderer label every PersistentVolumeClaim and
Namespace a flux-owned tag renders, and kustomize-controller then leaves the object serving
when it leaves the render.

`label` inserts the label as text, at the place the YAML parser marks, as `restart.stamp`
inserts `checksum/restart`, so no other byte of the file moves. A re-serialised document would
drop the quotes around a `"${key}"` and change the type Flux substitutes into it.

The plan is docs/target-architecture/deploy/d5-bulk-move.md, *Prune exemptions, digests and
the collision census (D5.1a1, D5.1a2)*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Iterable

import yaml

from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, RenderedTag
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.restart import _child, _insertion

PRUNE_LABEL = "kustomize.toolkit.fluxcd.io/prune"
PRUNE_DISABLED = "disabled"
# The kinds C3 exempts from prune.
EXEMPT_KINDS = frozenset({"PersistentVolumeClaim", "Namespace"})


def label(text: str, where: str) -> str:
    """`text` with the prune label on each PersistentVolumeClaim and Namespace it holds.

    Every other byte of `text` is kept. A document that already carries the label as
    `disabled` is left alone.

    Raises:
        RenderError: A document carries the label with another value, has no `metadata`, or
            its `metadata` or `labels` is in a shape `restart._insertion` cannot extend.
    """
    lines = text.splitlines(keepends=True)
    inserts = []
    entry = f"{PRUNE_LABEL}: {PRUNE_DISABLED}"
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        kind = _child(doc, "kind")
        if not (isinstance(kind, yaml.ScalarNode) and kind.value in EXEMPT_KINDS):
            continue
        metadata = _child(doc, "metadata")
        name = _child(metadata, "name")
        what = f"{where} {kind.value}/{getattr(name, 'value', '?')}"
        if metadata is None:
            raise RenderError(f"{what} has no metadata to carry {PRUNE_LABEL}")
        labels = _child(metadata, "labels")
        held = _child(labels, PRUNE_LABEL)
        if held is not None:
            if getattr(held, "value", None) != PRUNE_DISABLED:
                raise RenderError(
                    f"{what} carries {PRUNE_LABEL} as something other than "
                    f"{PRUNE_DISABLED}, and C3 exempts every one of its kind from prune"
                )
            continue
        if labels is None:
            inserts.append(_insertion(lines, metadata, ["labels:", f"  {entry}"], what))
        else:
            inserts.append(_insertion(lines, labels, [entry], what))
    for line, block in sorted(inserts, reverse=True):
        lines.insert(line, block)
    return "".join(lines)


def label_tags(rendered: list[RenderedTag], owned: Iterable[str]) -> list[RenderedTag]:
    """`rendered` with the prune label stamped into every directory of each flux-owned tag.

    DECIDED: only a flux-owned tag is labelled, as only one gets `checksum/restart`. R1's
    `shadow_diff.py compare` holds every other tag's render byte-equal to Ansible's, and the
    label means nothing to `kubectl apply`, so labelling those would turn the comparison red
    on every claim role for no effect. Both render modes label, so the round trip compares
    labelled with labelled.
    """
    owned = set(owned)
    labelled = []
    for tag in rendered:
        if tag.tag not in owned:
            labelled.append(tag)
            continue
        dirs = {
            name: {file: label(text, f"{name}/{file}") for file, text in files.items()}
            for name, files in tag.directories().items()
        }
        labelled.append(
            RenderedTag(tag.tag, dirs[tag.tag], dirs.get(tag.tag + DEFERRED_SUFFIX, {}))
        )
    return labelled
