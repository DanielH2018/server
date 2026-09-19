"""The `containers_list` half of `narrow_broad`: the entry diff, and which hits read the list.

Pure over text and dicts, so it imports nothing from `narrow_broad` and the two cannot cycle;
`narrow_broad._list_reader_tags` does the git reads and the tag mapping around `reader_paths`.
Split out on 2026-09-18 (#2044) when the reader census took `narrow_broad.py` past the
600-line cap.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from collections.abc import Callable, Iterable

from lib.render_guard import entry_tags

_MENTION = re.compile(r"(?<!\w)containers_list(?!\w)")
_JINJA_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def entries(doc: dict) -> dict[str, dict]:
    """`containers_list` keyed by service name."""
    found = doc.get("containers_list") or []
    return {e["name"]: e for e in found if isinstance(e, dict) and "name" in e}


def entry_change_tags(
    before: dict, after: dict, explain: Callable[[str], None], path: str
) -> set[str]:
    """The tags of the entries a `containers_list` change added or edited.

    An entry's own role is visited under the entry's tag. The list's READERS — a role whose
    template renders `containers_list` bare or through `hostvars[...]` — are the other half
    of the edge and are `narrow_broad._list_reader_tags`; until 2026-09-18 nothing mapped
    them (#2044): removing glances from daniel-pi's list changed monitor-bridge's
    `PI_PUBLISHED_PORTS`, no tag named the role, and its pod kept probing a port that no
    longer existed.

    A REMOVED entry contributes nothing of its own and refuses nothing. No `--tags` value
    applies a removal: the play iterates the list, so the removed entry's role is simply not
    visited, and the workload it left behind is reconciled by nothing (the same shape as
    `kubectl apply` leaving an orphaned object) — by the whole play just as much as by a
    narrowed one, so refusing bought no reconciliation and cost a full run plus every service
    marked stale (#2046). The journal names the orphan instead.
    """
    old, new = entries(before), entries(after)
    tags: set[str] = set()
    for name in sorted(set(old) - set(new)):
        explain(
            f"narrow: containers_list/{name} removed, its workload is reconciled by nothing"
            f" via {path}"
        )
    for name, entry in sorted(new.items()):
        if old.get(name) != entry:
            found = set(entry_tags(entry))
            explain(
                f"narrow: containers_list/{name} -> {','.join(sorted(found))} via {path}"
            )
            tags |= found
    return tags


def renders_containers_list(text: str) -> bool:
    """True when a template mentions `containers_list` inside `{{ }}` or `{% %}`.

    A mention anywhere else is prose: a `{# #}` header, a `k8s_autodeploy_reason` string, a
    YAML comment. Counting those reached 59 of 62 services on the real tree (2026-09-18),
    through `volume-claim`'s 25 callers and the `ingressroute` macro's importers, where the
    templates that read the list number four. `{# #}` is stripped first, because a header
    quotes code.
    """
    code = _JINJA_CODE.findall(_JINJA_COMMENT.sub("", text))
    return any(_MENTION.search(chunk) for chunk in code)


def reader_paths(
    hits: Iterable[str],
    skip_prefixes: tuple[str, ...],
    read: Callable[[str], str | None],
) -> list[str]:
    """The grep hits whose template renders the list, in the order given.

    A hit under `skip_prefixes` (the play's own tree) is not a consumer here: the play
    iterates the list to visit each entry's role, which the entry tags already name. A `.md`
    is prose and a `.py` under a role's `files/` runs on a host rather than rendering.
    """
    kept = []
    for hit in hits:
        if any(hit.startswith(p) for p in skip_prefixes) or hit.endswith(
            (".md", ".py")
        ):
            continue
        if renders_containers_list(read(hit) or ""):
            kept.append(hit)
    return kept
