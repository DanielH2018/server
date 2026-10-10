"""The YAML keys whose value Renovate rewrites, from ``renovate.json`` and its built-in managers.

A ``yaml`` atom hashes its key's value, so citing a key Renovate manages turns every bump of
that pin into a ``moved`` atom. The bump's PR then fails
``test_every_recorded_atom_hashes_as_recorded``, and neither Renovate nor its agent can run
``fact_status.py verify`` to clear it (#4012, four times by 2026-10-05). The lint rejects such
a citation instead. The section cites the file as a path atom and names the variable in prose.

The set is read from the config Renovate itself reads, never listed by hand: each
``customManagers`` entry's ``matchStrings`` runs over the files its ``managerFilePatterns``
select, and the line holding a ``currentValue`` or ``currentDigest`` capture names the
top-level key it sits under. A citation of a nested selector is flagged when its first segment
is a managed key, so a sibling of a pin inside the same mapping reads as managed too.

Renovate's built-in managers also rewrite YAML, and their file selection lives inside Renovate
rather than in this repo's config (#4158). ``_BUILT_IN`` restates the default
``managerFilePatterns`` of the two that select a tracked YAML file here, copied from
``lib/modules/manager/<name>/index.ts`` upstream, with the value lines each one extracts:

- ``github-actions`` reads ``uses:``, ``runs-on:``, ``container:`` and ``image:`` in workflow
  and action files, so every ``jobs`` (or ``runs``) selector is managed.
- ``ansible`` reads ``image:`` in any ``tasks/*.yml``.

docker-compose, helm-values and gitlabci select no tracked file, and the kubernetes manager has
no default pattern; add a row when one starts to. A built-in manager is active unless
``enabledManagers`` leaves it out or its own object sets ``"enabled": false``.

``image_pins`` answers a different question for the ``version-as-fact`` lint: which tag the
tree pins for each image, so a tag restated in prose can be compared with it.
"""

import fnmatch
import json
import re
from pathlib import Path

_JS_GROUP = re.compile(r"\(\?<(?=[A-Za-z])")
_TOP_KEY = re.compile(r"^([A-Za-z0-9_]+):")
_TOP_ITEM = re.compile(r"^-(?:\s|$)")
_PIN_GROUPS = ("currentValue", "currentDigest")

# name -> (default managerFilePatterns, a regex matching each line whose value it bumps)
_BUILT_IN = {
    "github-actions": (
        (
            "/(^|/)(workflow-templates|\\.(?:github|gitea|forgejo)/(?:workflows|actions))/.+\\.ya?ml$/",
            "/(^|/)action\\.ya?ml$/",
        ),
        # A local `uses: ./path` is a path, not a dependency, so nothing bumps it.
        re.compile(
            r"^\s*(?:-\s*)?(?:uses:\s*['\"]?(?!\.)\S|(?:runs-on|container|image):\s*\S)"
        ),
    ),
    "ansible": (
        ("/(^|/)tasks/[^/]+\\.ya?ml$/",),
        re.compile(r"^\s*(?:-\s*)?image:\s*\S"),
    ),
}


def _selects(pattern: str, rel: str) -> bool:
    """Renovate reads ``/…/`` as a regex over the path and anything else as a glob."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], rel) is not None
    return fnmatch.fnmatch(rel, pattern)


def _managers(
    config: Path,
) -> tuple[tuple[tuple[str, ...], tuple[re.Pattern, ...]], ...]:
    if not config.is_file():
        return ()
    out = []
    for m in json.loads(config.read_text(encoding="utf-8")).get("customManagers", []):
        # `fileMatch` is the pre-v38 spelling of the same selector, always a regex.
        patterns = tuple(m.get("managerFilePatterns", [])) + tuple(
            f"/{p}/" for p in m.get("fileMatch", [])
        )
        strings = tuple(
            re.compile(_JS_GROUP.sub("(?P<", s)) for s in m.get("matchStrings", [])
        )
        out.append((patterns, strings))
    return tuple(out)


def _built_ins(config: Path) -> tuple[tuple[tuple[str, ...], re.Pattern], ...]:
    if not config.is_file():
        return ()
    cfg = json.loads(config.read_text(encoding="utf-8"))
    enabled = cfg.get("enabledManagers")
    return tuple(
        spec
        for name, spec in _BUILT_IN.items()
        if (enabled is None or name in enabled)
        and cfg.get(name, {}).get("enabled", True) is not False
    )


def _key_above(lines: list[str], index: int) -> str | None:
    """The first selector segment owning line ``index``: a top-level key, or the item's index.

    An Ansible tasks file is a list at the root, and a citation into it starts with the item
    index (``0.community.docker.docker_container.image``), so a ``- `` item at column 0 keys
    by its position among the root items.
    """
    for i in range(index, -1, -1):
        if m := _TOP_KEY.match(lines[i]):
            return m.group(1)
        if _TOP_ITEM.match(lines[i]):
            return str(sum(1 for line in lines[:i] if _TOP_ITEM.match(line)))
    return None


def pinned_keys(repo: Path, rel: str) -> frozenset[str]:
    """The top-level keys of ``rel`` whose value a Renovate manager rewrites."""
    target = repo / rel
    if not target.is_file():
        return frozenset()
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    keys: set[str] = set()
    for patterns, strings in _managers(repo / "renovate.json"):
        if not any(_selects(p, rel) for p in patterns):
            continue
        for rx in strings:
            for m in rx.finditer(text):
                for group in _PIN_GROUPS:
                    if group in rx.groupindex and m.start(group) >= 0:
                        key = _key_above(lines, text.count("\n", 0, m.start(group)))
                        if key:
                            keys.add(key)
    for patterns, line_rx in _built_ins(repo / "renovate.json"):
        if not any(_selects(p, rel) for p in patterns):
            continue
        for i, line in enumerate(lines):
            if line_rx.match(line) and (key := _key_above(lines, i)):
                keys.add(key)
    return frozenset(keys)


# ── image pins, for the `version-as-fact` lint ───────────────────────────────────────────
#
# Every image the tree pins by tag, read from where the tree pins it: the top-level `*_image`
# keys in role defaults and inventory vars, and the `image:` lines in the Pi's compose
# templates, whose pins live nowhere else. A value holding `{{` is a built image whose tag
# the play computes, so it pins nothing a sentence could restate.
_VAR_FILES = re.compile(
    r"^ansible/(?:roles/[^/]+/[^/]+/defaults/[^/]+|inventory/(?:group|host)_vars/[^/]+)\.ya?ml$"
)
_COMPOSE_FILES = re.compile(
    r"^ansible/roles/containers/[^/]+/templates/docker-compose\.ya?ml\.j2$"
)
_IMAGE_VAR = re.compile(r"^(?P<var>[a-z0-9_]+_image):\s*['\"]?(?P<ref>[^\s'\"]+)", re.M)
_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*['\"]?(?P<ref>[^\s'\"]+)", re.M)


def split_image_ref(ref: str) -> tuple[str, str] | None:
    """``(name, tag)`` of an ``image:tag[@digest]`` reference, or None when it carries no tag.

    The tag colon is the last one after the last slash, so a registry port
    (``host:5000/img:1.0``) stays part of the name.
    """
    ref = ref.split("@", 1)[0]
    colon = ref.rfind(":")
    if colon <= ref.rfind("/") or colon in (0, len(ref) - 1):
        return None
    return ref[:colon], ref[colon + 1 :]


def image_pins(
    repo: Path, tracked: frozenset[str]
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Image name -> every ``(tag, variable, file)`` that pins it, in file then line order.

    A compose pin has no variable, so its second field reads ``image:``.
    """
    found: dict[str, list[tuple[str, str, str]]] = {}
    for rel in sorted(tracked):
        if _VAR_FILES.match(rel):
            rx = _IMAGE_VAR
        elif _COMPOSE_FILES.match(rel):
            rx = _COMPOSE_IMAGE
        else:
            continue
        for m in rx.finditer((repo / rel).read_text(encoding="utf-8")):
            ref = m.group("ref")
            parts = None if "{{" in ref else split_image_ref(ref)
            if parts:
                var = m.groupdict().get("var") or "image:"
                found.setdefault(parts[0], []).append((parts[1], var, rel))
    return {name: tuple(pins) for name, pins in found.items()}
