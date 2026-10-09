"""The `# renovate:` annotations in the roles' defaults, read the way Renovate reads them.

A pinned version in a role's `defaults/main.yml` is declared to Renovate by one comment above
its `_version:` line:

    # renovate: datasource=github-releases depName=getsops/sops
    sops_setup_version: "v3.13.3"

One generic custom manager in renovate.json reads every such pair. It carries the
`datasource`, `depName`, `versioning`, `extractVersion` and `registryUrl` capture groups, so
the annotation supplies what a per-pin manager used to carry as `*Template` fields.

The checks that reason about a pin per dependency (which role owns it, which extractVersion it
uses, which registry it reads) were written against those per-pin managers. `expand` turns each
annotation back into one: it runs the generic manager's own regex over the files its own
`managerFilePatterns` select, and builds a manager-shaped dict from the groups the match
captured. Those checks then read an annotated pin and a hand-written manager the same way, and
what they read is what the config matches rather than a second parser's opinion of it.

Renovate's patterns are RE2 with `(?<name>...)` groups; `to_python_regex` converts the group
syntax, which is the only difference these patterns use.
"""

import re
from collections.abc import Callable, Iterable

# The capture groups that make a manager an annotation reader: the datasource comes from the
# matched text instead of a `datasourceTemplate`.
_ANNOTATION_GROUP = "(?<datasource>"

# Annotation group -> the `*Template` field a per-pin manager sets for the same thing.
_TEMPLATE_FIELD = {
    "datasource": "datasourceTemplate",
    "depName": "depNameTemplate",
    "versioning": "versioningTemplate",
    "extractVersion": "extractVersionTemplate",
    "registryUrl": "registryUrlTemplate",
}

# The pinned variable is the last line of an annotation match: `<name>_version: <value>`.
_VARIABLE_LINE = re.compile(r"^\s*([A-Za-z0-9_]+):", re.MULTILINE)


def to_python_regex(pattern: str) -> str:
    """A Renovate/RE2 pattern with `(?<name>` groups rewritten to Python's `(?P<name>`."""
    return re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern)


def file_pattern_regex(pattern: str) -> re.Pattern[str]:
    """A `/regex/` managerFilePatterns entry as a compiled Python regex."""
    assert pattern.startswith("/") and pattern.endswith("/"), (
        f"expected a /regex/ file pattern: {pattern}"
    )
    return re.compile(pattern[1:-1])


def is_annotation_manager(manager: dict) -> bool:
    """True for a custom manager whose datasource comes from an annotation in the file."""
    return any(_ANNOTATION_GROUP in ms for ms in manager.get("matchStrings", []))


def annotations_in(manager: dict, text: str) -> list[dict[str, str]]:
    """Each annotation `manager` matches in `text`, as its captured groups plus `variable`.

    `variable` is the key on the pinned line. Groups the match did not capture are left out,
    so an annotation with no `versioning=` falls back to the datasource's default exactly as
    Renovate's does.
    """
    found = []
    for ms in manager["matchStrings"]:
        for match in re.finditer(to_python_regex(ms), text):
            groups = {k: v for k, v in match.groupdict().items() if v is not None}
            groups["variable"] = _VARIABLE_LINE.findall(match.group(0))[-1]
            found.append(groups)
    return found


def expand(
    manager: dict, files: Iterable[str], read: Callable[[str], str]
) -> list[dict]:
    """One per-pin manager dict for each annotation `manager` matches in `files`.

    Each dict scans exactly the annotated file, matches exactly the annotated line, and names
    its dependency in the `*Template` fields a hand-written per-pin manager uses. `annotation`
    records where it came from, which is how a reader tells the two apart.
    """
    patterns = [file_pattern_regex(p) for p in manager["managerFilePatterns"]]
    expanded = []
    for path in files:
        if not any(p.search(path) for p in patterns):
            continue
        for groups in annotations_in(manager, read(path)):
            variable = groups["variable"]
            pin = {
                "customType": "regex",
                "description": f"`# renovate:` annotation on {variable} in {path}",
                "managerFilePatterns": [f"/^{re.escape(path)}$/"],
                "matchStrings": [f'{variable}:\\s*"?(?<currentValue>[^"\\s]+)"?'],
                "annotation": {"file": path, "variable": variable},
            }
            for group, field in _TEMPLATE_FIELD.items():
                if group in groups:
                    pin[field] = groups[group]
            if "depTypeTemplate" in manager:
                pin["depTypeTemplate"] = manager["depTypeTemplate"]
            expanded.append(pin)
    return expanded


def effective_managers(
    managers: list[dict], files: Iterable[str], read: Callable[[str], str]
) -> list[dict]:
    """`managers` with each annotation reader replaced by the per-pin managers it expands to."""
    files = list(files)
    out: list[dict] = []
    for manager in managers:
        if is_annotation_manager(manager):
            out.extend(expand(manager, files, read))
        else:
            out.append(manager)
    return out
