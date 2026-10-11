"""The `containers_list` half of `narrow_lib/broad.py`: the entry diff, and which hits read it.

Pure over text and dicts, so it imports nothing from `broad` and the two cannot cycle;
`broad._list_reader_tags` does the git reads and the tag mapping around `reader_paths`.

An entry edit reaches only the readers of the fields it changed (#4340), and of the entry it
changed where a reader names one (#4388). `changed_fields` names each edited entry's fields and
`reader_reads` the fields a template reads, keyed by the entry its filter looks up. Either side
answers None for "every field" when it cannot name them, and None on either side keeps the
reader, as before #4340.
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
# own source. It fails when a row omits a key the filter reads, and when the filter reads an
# entry by any route other than a literal key (a computed key, a loop over the entry, handing
# it to a call), unless that test vets the route.
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
# The filters that read one entry, the one their first argument names. Called with a literal
# name, such a filter reads that entry alone, so an edit to any other entry cannot move it.
# Called with anything else it may read any entry. Two filters that take a name stay out:
# `metrics_port` takes a job, found by walking every entry's `metrics`, so another entry can
# declare it first; `in_service_tier` runs `tier_entries`, which raises on any entry's unknown
# tier. `test_narrow_containers_fields.py` holds each of these to its filter's behaviour.
NAMED_ENTRY = frozenset(
    {"entry_port", "homepage_href", "homepage_widget_url", "tier_priority_class"}
)
# Filters that hand the list on unchanged, so the filter after them is the one that reads it.
_PASS_THROUGH = frozenset({"default"})
# A first argument that is one string literal and nothing else: `('docs', domain)`, `('sonarr')`.
_LITERAL_NAME = re.compile(r"\(\s*(?:'([^'\\]*)'|\"([^\"\\]*)\")\s*[,)]")


def entries(doc: dict) -> dict[str, dict]:
    """`containers_list` keyed by service name."""
    return {e["name"]: e for e in containers_entries_in(doc)}


def changed_fields(before: dict, after: dict) -> dict[str, frozenset[str]] | None:
    """The fields a `containers_list` edit changed, by entry; None when it moves every reader.

    An entry the edit left alone is absent. An added or removed entry moves every reader that
    iterates the list, whatever it reads, and a renamed entry is one of each. A reorder moves
    them too, with no field changed: `scrape_jobs` and `kuma_ingress_monitors` emit in list
    order. A name two entries share is every field as well: `entries` keeps the last of them
    and a `NAMED_ENTRY` filter reads the first, so an edit to the first would go unseen.
    """
    old, new = entries(before), entries(after)
    repeated = any(
        len(entries(doc)) != len(containers_entries_in(doc)) for doc in (before, after)
    )
    if repeated or list(old) != list(new):
        return None
    changed = {}
    for name, entry in new.items():
        keys = frozenset(
            key
            for key in set(entry) | set(old[name])
            if entry.get(key) != old[name].get(key)
        )
        if keys:
            changed[name] = keys
    return changed


def reader_reads(text: str) -> dict[str | None, frozenset[str]] | None:
    """The entry fields a template reads through `containers_list`, by entry; None for all.

    The key is the entry a `NAMED_ENTRY` filter looks up by a literal name, or None for a read
    that may land on any entry. Every mention inside Jinja code must hand the list straight to
    a filter `FILTER_FIELDS` names, past any `default`. One mention that does not (a loop,
    `selectattr`, an index, a filter returning entries) makes the whole template a reader of
    every field.
    """
    reads: dict[str | None, frozenset[str]] = {}
    for chunk in _JINJA_CODE.findall(_JINJA_COMMENT.sub("", text)):
        for mention in _MENTION.finditer(chunk):
            name, pos = _filter_after(chunk, mention.end())
            if name not in FILTER_FIELDS:
                return None
            entry = _literal_name(chunk, pos) if name in NAMED_ENTRY else None
            reads[entry] = reads.get(entry, frozenset()) | FILTER_FIELDS[name]
    return reads


def _filter_after(code: str, pos: int) -> tuple[str | None, int]:
    """The first filter applied at `pos` that is not a pass-through, and where its name ends.

    The name is None when no filter is applied there.
    """
    while match := _FILTER.match(code, pos):
        if match.group(1) not in _PASS_THROUGH:
            return match.group(1), match.end()
        pos = _past_arguments(code, match.end())
    return None, pos


def _literal_name(code: str, pos: int) -> str | None:
    """The first argument of the call at `pos` when it is a string literal, else None."""
    match = _LITERAL_NAME.match(code, pos)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


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


def describe(changed: dict[str, frozenset[str]] | None) -> str:
    """`changed` for the journal's derivation line: its fields, and how many entries moved.

    The `containers_list/<name>` lines `entry_change_tags` writes name the entries.
    """
    if changed is None:
        return "every field"
    if not changed:
        return "no field"
    fields = ",".join(sorted(frozenset().union(*changed.values())))
    return f"{fields} on {len(changed)} {'entry' if len(changed) == 1 else 'entries'}"


def reads_changed(
    reads: dict[str | None, frozenset[str]], changed: dict[str, frozenset[str]]
) -> bool:
    """Whether a template's `reads` meet the fields an edit `changed`.

    A read keyed by an entry meets only that entry's changes, and an unkeyed read meets any
    entry's.
    """
    anywhere = frozenset().union(*changed.values())
    return any(
        fields & (anywhere if entry is None else changed.get(entry, frozenset()))
        for entry, fields in reads.items()
    )


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
    changed: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """The grep hits whose template reads the fields of `changed`, in the order given.

    `changed` maps each edited entry to its changed fields, as `changed_fields` returns it.
    None is every field of every entry, so every hit that renders the list is kept.

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
        reads = None if changed is None else reader_reads(text)
        if reads is None or changed is None or reads_changed(reads, changed):
            kept.append(hit)
    return kept
