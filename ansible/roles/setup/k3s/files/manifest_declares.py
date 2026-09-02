#!/usr/bin/env python3
"""What the staged manifests declare — managed by Ansible (k3s role); edits overwritten.

Feeds the orphan arm of `manifest-prune-check.sh`, which asks of every live object: does any
staged manifest still declare this? Until this script existed that question was answered by

    grep -rlE "name: ${name}[[:space:]]*$" /etc/rancher/k3s/manifests

and the script's own header recorded the resulting false negative: a container, a volume, a
port or a ConfigMap key sharing a name with the orphaned object reads as "still declared" — in
ANY staged file, of ANY kind. The orphan is then missed, silently, by the check whose whole
purpose is to find it.

The failure is structural. `name:` appears at a dozen nesting levels in a Kubernetes manifest,
and a match that ignores position cannot tell a top-level `metadata.name` from a container's.
So this reads position: a document's `kind` and its `metadata:` block are the only two mappings
at column 0, and the object's own name is the `name:` two spaces into that block. A container's
name lives under `spec.template.spec.containers`, four levels deeper, and never matches.

WHY NOT PyYAML. The host cron runs `uv run --no-project`, which supplies no third-party
packages — the sibling `live_drift_check.py` is stdlib-only for the same reason. Adding a
dependency to a root cron whose failure mode is a false all-clear is not worth the parser.
`test_manifest_declares.py` closes the gap the other way: it parses every rendered manifest in
the repo with PyYAML and asserts this reader returns the same set, so a shape this parser
cannot read fails the suite rather than going quiet on the host.

WHY KIND AND NAME, NOT NAMESPACE TOO. `kubectl apply -f <dir>/` takes no `-n`, so an object
whose manifest omits `metadata.namespace` lands wherever the file's context puts it, and that
cannot be reconstructed from the file alone. Matching the pair we can read exactly is strictly
tighter than the any-name-anywhere match it replaces and never invents a namespace it would
then get wrong. A cross-namespace collision — same kind, same name, two namespaces, one
retired — still reads as declared. That is a narrower hole than the one it replaces.

Usage:
    manifest-declares.py /etc/rancher/k3s/manifests
Prints one `kind/name` per line, kind lowercased to match kubectl's spelling.
"""

from __future__ import annotations

import sys
from pathlib import Path

SUFFIXES = (".yaml", ".yml")

# A YAML document break. `kind:` and `metadata:` are per-document, so the accumulator resets
# here or a multi-document file would pair one document's kind with another's name.
DOC_BREAK = "---"


def _scalar(line: str) -> str:
    """The value after `key:`, stripped of quotes and any trailing comment."""
    value = line.split(":", 1)[1].strip()
    if value and value[0] in "\"'" and value[-1] == value[0] and len(value) > 1:
        return value[1:-1]
    # An unquoted scalar cannot contain ` #`, which starts a comment.
    return value.split(" #", 1)[0].strip()


def declared_in(text: str) -> set[str]:
    """Every `kind/name` the YAML documents in `text` declare at the top level."""
    names: set[str] = set()
    kind: str | None = None
    name: str | None = None
    in_metadata = False

    def flush():
        nonlocal kind, name, in_metadata
        if kind and name:
            names.add(f"{kind.lower()}/{name}")
        kind = name = None
        in_metadata = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith(DOC_BREAK):
            flush()
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            # Any other column-0 key ends the metadata block: `spec:` and `data:` are siblings
            # of `metadata:`, and their nested `name:` keys must not be read as the object's.
            in_metadata = line.startswith("metadata:")
            if line.startswith("kind:"):
                kind = _scalar(line)
            continue
        if in_metadata and indent == 2 and line.lstrip().startswith("name:"):
            name = _scalar(line)
    flush()
    return names


def declared(root: Path) -> tuple[set[str], list[str]]:
    """({"kind/name", ...}, [read errors]) across every staged manifest under `root`."""
    names: set[str] = set()
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        try:
            names |= declared_in(path.read_text())
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return names, errors


def main(argv: list[str]) -> int:
    """Print every `kind/name` declared under `argv[1]`, one per line, sorted.

    Exits 64 on a bad usage, 66 if the given root isn't a directory, and 1 if the root yielded
    no declared objects at all — an empty result must not read as "no orphans", so this is a
    read failure the caller reports rather than the several hundred orphans an empty set would
    silently imply. Any unreadable manifest is printed to stderr but does not fail the run by
    itself. Returns 0 on success.
    """
    if len(argv) != 2:
        print(f"usage: {argv[0]} <manifest-root>", file=sys.stderr)
        return 64
    root = Path(argv[1])
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 66
    names, errors = declared(root)
    for line in errors:
        print(f"unread: {line}", file=sys.stderr)
    if not names:
        # An empty set would mark every live object an orphan. The caller checks this exit and
        # reports the read failure rather than the several hundred orphans it implies.
        print(f"{root} yielded no declared objects", file=sys.stderr)
        return 1
    for name in sorted(names):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
