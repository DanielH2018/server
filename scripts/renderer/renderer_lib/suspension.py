"""The rule against `suspend: false` on a Flux object (C3 *Suspension*, D5.1b).

kustomize-controller applies with forced ownership, so a rendered `spec.suspend: false` would
take the field back from an operator's hand suspend. The renderer therefore writes the field
only when it is true, and `bundle.bundle_files` fails a render whose bundle holds a `false` in
any document, a role template's included. The flux role's `sync.yaml.j2` follows the same rule.
`suspends_tag` reads the rendered flag back for `digests.json`'s `suspended` field (D5.1a2).
The plan is docs/target-architecture/deploy/d5-bulk-move.md, *The stop lever (D5.1b)*.

The flux role's `flux_sync_suspend` is refused outright from D5 step 29 on
(`sync_suspend_refusal`). Flux applies its own sync objects from then, so a suspend rendered
there would stop the OCIRepository that would have to fetch the commit clearing it.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Mapping

import yaml

from lib import yaml_fast

# The flux role's old lever over its sync objects, refused when true (D5 step 29).
SYNC_SUSPEND_KEY = "flux_sync_suspend"

# Every Flux kind's API group ends in this. A Job's or CronJob's `spec.suspend` is another
# field and stays free to read `false`.
FLUX_API_GROUP_SUFFIX = ".toolkit.fluxcd.io"


def suspends_tag(text: bytes | None) -> bool:
    """Whether a tag's `flux/<tag>.yaml` suspends its first document, the Kustomization `<tag>`.

    Only `spec.suspend: true` counts, since the renderer writes the field only when it is
    true (C3 *Suspension*). A tag with no file reads as not suspended.
    """
    if text is None:
        return False
    first = next(iter(yaml_fast.safe_load_all(text.decode())), None)
    spec = first.get("spec") if isinstance(first, dict) else None
    return isinstance(spec, dict) and spec.get("suspend") is True


def suspend_false(files: Mapping[str, str]) -> list[str]:
    """Each Flux object in `files` whose `spec.suspend` is false, as `<path> <kind>/<name>`.

    Only a kind whose API group ends in `FLUX_API_GROUP_SUFFIX` counts. A file that is not
    YAML, such as the `cluster-vars` dotenv copy, is skipped, and so is one that does not
    parse: the publish census owns that failure.
    """
    found = []
    for path in sorted(files):
        if not path.endswith((".yaml", ".yml")):
            continue
        try:
            docs = list(yaml_fast.safe_load_all(files[path]))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            group = str(doc.get("apiVersion", "")).split("/")[0]
            spec = doc.get("spec")
            if (
                group.endswith(FLUX_API_GROUP_SUFFIX)
                and isinstance(spec, dict)
                and spec.get("suspend") is False
            ):
                name = (doc.get("metadata") or {}).get("name", "?")
                found.append(f"{path} {doc.get('kind')}/{name}")
    return found


def refuse_suspend_false(files: Mapping[str, str]) -> None:
    """Raise when any Flux object in `files` sets `spec.suspend: false`.

    Raises:
        ValueError: naming each such object (`suspend_false`).
    """
    found = suspend_false(files)
    if found:
        raise ValueError(
            "the bundle sets suspend: false on "
            + ", ".join(found)
            + ". Write spec.suspend only when it is true: kustomize-controller applies with "
            "forced ownership, so a rendered false undoes an operator's hand suspend"
        )


def sync_suspend_refusal(tag: str, value: bool) -> str | None:
    """Why `tag`'s render is refused when its resolved `flux_sync_suspend` is true, else None.

    Args:
        tag: The deploy tag being rendered.
        value: The key's value in the tag's resolved context, read through Ansible's `bool`,
            so a host_vars or extra-vars `true` is refused as a defaults one is.
    """
    if not value:
        return None
    return (
        f"{tag}: {SYNC_SUSPEND_KEY} is true, which the renderer refuses from D5 step 29 on. "
        "Flux applies its own sync objects, so it would suspend its own source and could "
        "never fetch the commit that clears the key. To stop everything, suspend the "
        "OCIRepository homelab by hand (docs/flux-deploy-plane.md, *Recovering Flux by hand*)"
    )
