"""The `# renovate:` annotations in the roles' defaults, read the way Renovate reads them.

A pinned version in a role's `defaults/main.yml` is declared to Renovate by one comment above
its `_version:` line:

    # renovate: datasource=github-releases depName=coredns/coredns
    k3s_host_coredns_version: "1.12.0"

One generic custom manager in renovate.json reads every such pair. It carries the
`datasource`, `depName`, `versioning`, `extractVersion` and `registryUrl` capture groups, so
the annotation supplies what a per-pin manager used to carry as `*Template` fields.

`asset_pins.py` reads them here to learn which depName each pinned download renders from,
because its `--refresh` runs where Renovate does not. CI's extract check
(`scripts/validate/renovate_extract.py:annotation_reader_drift`) holds what this reads to
what Renovate's own extract reads.

Renovate's patterns are RE2 with `(?<name>...)` groups; `to_python_regex` converts the group
syntax, which is the only difference these patterns use.
"""

import re

# The capture groups that make a manager an annotation reader: the datasource comes from the
# matched text instead of a `datasourceTemplate`.
_ANNOTATION_GROUP = "(?<datasource>"

# The pinned variable is the last line of an annotation match: `<name>_version: <value>`.
_VARIABLE_LINE = re.compile(r"^\s*([A-Za-z0-9_]+):", re.MULTILINE)


def to_python_regex(pattern: str) -> str:
    """A Renovate/RE2 pattern with `(?<name>` groups rewritten to Python's `(?P<name>`."""
    return re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern)


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
