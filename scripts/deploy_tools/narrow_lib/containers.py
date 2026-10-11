"""The `containers_list` half of `narrow_lib/broad.py`: the entry diff, and which hits read it.

Pure over text and dicts, so it imports nothing from `broad` and the two cannot cycle;
`broad._list_reader_tags` does the git reads and the tag mapping around `reader_paths`.

An entry edit reaches only the readers of the fields it changed (#4340). `changed_fields` names
an edit's fields and `reader_fields` a template's. Either side answers None for "every field"
when it cannot name them, and None on either side keeps the reader, as before #4340.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re
from collections.abc import Callable, Iterable

from lib.ansible_inventory import containers_entries_in
from lib.render_guard import entry_tags

_MENTION = re.compile(r"(?<!\w)containers_list(?!\w)")
_JINJA_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_FILTER = re.compile(r"\s*\|\s*(\w+)")

# The entry fields each filter reads when a template hands it `containers_list`. A filter absent
# here reads every field: `filter_by_platform` and `selectattr` return whole entries, and the
# template then reads whatever it likes from them. `name` is in every row because each filter
# finds its entry by name. `test_narrow_containers_fields.py` derives each row from the filter's
# own source and fails when a row omits a key the filter reads.
FILTER_FIELDS: dict[str, frozenset[str]] = {
    "authelia_service_rules": frozenset(
        {"auth_tier", "hostname", "name", "networks", "use_authelia"}
    ),
    "entry_port": frozenset({"name", "port"}),
    "homepage_href": frozenset({"hostname", "name"}),
    "homepage_widget_url": frozenset({"homepage_widget", "name", "port"}),
    "in_service_tier": frozenset({"name", "tier"}),
    "kuma_ingress_monitors": frozenset({"auth_tier", "hostname", "id", "kuma", "name"}),
    "metrics_port": frozenset({"job", "metrics", "name", "params", "port"}),
    "scrape_jobs": frozenset({"job", "metrics", "name", "params", "port", "service"}),
    "tier_priority_class": frozenset({"name", "tier"}),
}
# Filters that hand the list on unchanged, so the filter after them is the one that reads it.
_PASS_THROUGH = frozenset({"default"})


def entries(doc: dict) -> dict[str, dict]:
    """`containers_list` keyed by service name."""
    return {e["name"]: e for e in containers_entries_in(doc)}


def changed_fields(before: dict, after: dict) -> frozenset[str] | None:
    """The entry fields a `containers_list` edit changed; None when it added or removed an entry.

    An added or removed entry moves every reader that iterates the list, whatever it reads,
    and a renamed entry is one of each.
    """
    old, new = entries(before), entries(after)
    if set(old) != set(new):
        return None
    return frozenset(
        key
        for name, entry in new.items()
        for key in set(entry) | set(old[name])
        if entry.get(key) != old[name].get(key)
    )


def reader_fields(text: str) -> frozenset[str] | None:
    """The entry fields a template reads through `containers_list`; None for every field.

    Every mention inside Jinja code must hand the list straight to a filter `FILTER_FIELDS`
    names, past any `default`. One mention that does not (a loop, `selectattr`, an index, a
    filter returning entries) makes the whole template a reader of every field.
    """
    fields: set[str] = set()
    for chunk in _JINJA_CODE.findall(_JINJA_COMMENT.sub("", text)):
        for mention in _MENTION.finditer(chunk):
            name = _filter_after(chunk, mention.end())
            if name not in FILTER_FIELDS:
                return None
            fields |= FILTER_FIELDS[name]
    return frozenset(fields)


def _filter_after(code: str, pos: int) -> str | None:
    """The first filter applied at `pos` that is not a pass-through, or None for no filter."""
    while match := _FILTER.match(code, pos):
        if match.group(1) not in _PASS_THROUGH:
            return match.group(1)
        pos = _past_arguments(code, match.end())
    return None


def _past_arguments(code: str, pos: int) -> int:
    """The position after a parenthesised argument list starting at `pos`, if one does."""
    if not code.startswith("(", pos):
        return pos
    depth = 0
    for i in range(pos, len(code)):
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        if depth == 0:
            return i + 1
    return len(code)


def describe(fields: frozenset[str] | None) -> str:
    """`fields` for the journal's derivation line."""
    if fields is None:
        return "every field"
    return ",".join(sorted(fields)) or "no field"


def entry_change_tags(
    before: dict, after: dict, explain: Callable[[str], None], path: str
) -> set[str]:
    """The tags of the entries a `containers_list` change added or edited.

    An entry's own role is visited under the entry's tag. The list's READERS — a role whose
    template renders `containers_list` bare or through `hostvars[...]` — are the other half
    of the edge and are `narrow_broad._list_reader_tags`: removing glances from daniel-pi's
    list changes monitor-bridge's `PI_PUBLISHED_PORTS`, though no tag names that role.

    A REMOVED entry contributes nothing of its own and refuses nothing. No `--tags` value
    applies a removal: the play iterates the list, so the removed entry's role is simply not
    visited, and the workload it left behind is reconciled by nothing (the same shape as
    `kubectl apply` leaving an orphaned object) — by the whole play just as much as by a
    narrowed one, so refusing bought no reconciliation and cost a full run plus every service
    marked stale. The journal names the orphan instead.
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
    YAML comment. Counting those reached 59 of 62 services on the real tree,
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
    changed: frozenset[str] | None = None,
) -> list[str]:
    """The grep hits whose template reads `changed` fields of the list, in the order given.

    `changed` None is every field, so every hit that renders the list is kept.

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
        text = read(hit) or ""
        if not renders_containers_list(text):
            continue
        fields = None if changed is None else reader_fields(text)
        if fields is None or changed is None or fields & changed:
            kept.append(hit)
    return kept
