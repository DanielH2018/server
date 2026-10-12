"""The `containers_list` half of `narrow_lib/broad.py`: the entry diff, and which hits read it.

Pure over text and dicts, so it imports nothing from `broad` and the two cannot cycle;
`broad._list_reader_tags` does the git reads and the tag mapping around `reader_paths`.

An entry edit reaches only the readers of the fields it changed (#4340), and of the entry it
changed where a reader names one (#4388). `changed_fields` names each edited entry's fields and
`reader_reads` the fields a template reads, keyed by the entry its filter looks up, or by the
job a `metrics_port` read names, which `reads_changed` resolves to the entry declaring it
(#4396). Either side answers None for "every field" when it cannot name them, and None on
either side keeps the reader, as before #4340. A read through `hostvars['<host>']` with a
literal host reads that host's list alone, so an edit to another host's `host_vars` skips it
(#4422).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re
from collections.abc import Callable, Iterable, Mapping
from typing import NamedTuple

from lib.ansible_inventory import containers_entries_in
from lib.render_guard import entry_tags

_MENTION = re.compile(r"(?<!\w)containers_list(?!\w)")
_JINJA_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_FILTER = re.compile(r"\s*\|\s*(\w+)")
# `hostvars['daniel-pi'].` ending right before a mention: the host whose list it reads.
_HOSTVARS_LITERAL = re.compile(
    r"(?<!\w)hostvars\s*\[\s*(?:'([^'\\]*)'|\"([^\"\\]*)\")\s*\]\s*\.\s*$"
)
_HOST_VARS_PATH = re.compile(r"(?:^|/)host_vars/([^/]+?)(?:\.ya?ml)?(?:/|$)")

# The entry fields each filter reads when a template hands it `containers_list`. A filter absent
# here reads every field: `filter_by_platform` and `selectattr` return whole entries, and the
# template then reads whatever it likes from them. A filter that looks an entry up by name has
# `name` in its row. `test_narrow_filter_tables.py` derives each row from the filter's
# own source. It fails when a row omits a key the filter reads, and when the filter reads an
# entry by any route other than a literal key (a computed key, a loop over the entry, handing
# it to a call), unless that test vets the route.
FILTER_FIELDS: dict[str, frozenset[str]] = {
    "authelia_service_rules": frozenset(
        {"auth_tier", "hostname", "name", "networks", "use_authelia"}
    ),
    "entry_names": frozenset({"name"}),
    "entry_port": frozenset({"name", "port"}),
    "homepage_href": frozenset({"hostname", "name"}),
    "homepage_widget_url": frozenset({"homepage_widget", "name", "port"}),
    "in_service_tier": frozenset({"name", "tier"}),
    "kuma_ingress_monitors": frozenset({"auth_tier", "hostname", "id", "kuma", "name"}),
    "lan_hostnames": frozenset({"extra_hostnames", "hostname", "platform"}),
    "metrics_port": frozenset({"job", "metrics", "name", "params", "port"}),
    # `app`, `from`, `port` and `role_owned` are a `netpol_fences` item's keys too.
    "netpol_caller_fences": frozenset(
        {
            "app",
            "from",
            "homepage_widget",
            "name",
            "netpol_app",
            "netpol_fences",
            "netpol_from",
            "netpol_role_owned",
            "port",
            "role_owned",
        }
    ),
    "published_ports": frozenset({"name", "port"}),
    "scrape_jobs": frozenset({"job", "metrics", "name", "params", "port", "service"}),
    "tier_priority_class": frozenset({"name", "tier"}),
}
# The filters that read one entry, the one their first argument names. Called with a literal
# name, such a filter reads that entry alone, so an edit to any other entry cannot move it.
# Called with anything else it may read any entry. Two filters that take a name stay out:
# `metrics_port` takes a job, which `NAMED_JOB` covers; `in_service_tier` runs `tier_entries`,
# which raises on any entry's unknown tier. `test_narrow_filter_tables.py` holds each of
# these to its filter's behaviour.
NAMED_ENTRY = frozenset(
    {"entry_port", "homepage_href", "homepage_widget_url", "tier_priority_class"}
)
# The filters whose first argument names a `metrics` job. The filter walks every entry's
# `metrics` to the first that declares the job, so while no entry's `metrics` changed it
# reads that entry alone, and `reads_changed` keys the read on it.
NAMED_JOB = frozenset({"metrics_port"})


class Job(NamedTuple):
    """A `reader_reads` key: the read lands on whichever entry declares this `metrics` job.

    A distinct type, so the job `crowdsec` cannot collide with the entry `crowdsec`.
    """

    name: str


# Filters that hand the list on unchanged, so the filter after them is the one that reads it.
_PASS_THROUGH = frozenset({"default"})
# A first argument that is one string literal and nothing else: `('docs', domain)`, `('sonarr')`.
_LITERAL_NAME = re.compile(r"\(\s*(?:'([^'\\]*)'|\"([^\"\\]*)\")\s*[,)]")


def entries(doc: dict) -> dict[str, dict]:
    """`containers_list` keyed by service name."""
    return {e["name"]: e for e in containers_entries_in(doc)}


def job_owners(doc: dict) -> dict[str, str | None]:
    """Each `metrics` job to the entry `metrics_port` answers it from: the first to declare it.

    The owner is None for an entry with no name, which `entries` cannot key, so a read of its
    job stays unkeyed. A malformed `metrics` value is skipped here. `metrics_port` raises on
    it, before and after an edit that leaves `metrics` alone, so no owner it would name
    changes either.
    """
    owners: dict[str, str | None] = {}
    for entry in (doc or {}).get("containers_list") or []:
        items = entry.get("metrics") if isinstance(entry, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "job" in item:
                owners.setdefault(item["job"], entry.get("name") or None)
    return owners


class Edit(NamedTuple):
    """A `containers_list` edit, as `reader_paths` matches it against each template's reads.

    Attributes:
        changed: the fields the edit changed, by entry, as `changed_fields` returns them.
        owners: each `metrics` job's entry after the edit, as `job_owners` returns them.
        host: the host whose list the edit changed, as `edited_host` returns it; None when
          the file is not one host's, so every host's list counts as edited.
    """

    changed: dict[str, frozenset[str]] | None
    owners: dict[str, str | None]
    host: str | None = None


def edit(before: dict, after: dict, path: str = "") -> Edit:
    """The `Edit` that turns the inventory document `before` at `path` into `after`."""
    return Edit(changed_fields(before, after), job_owners(after), edited_host(path))


def edited_host(path: str) -> str | None:
    """The host a `host_vars/<host>.yml` or `host_vars/<host>/...` file belongs to, else None."""
    match = _HOST_VARS_PATH.search(path)
    return match.group(1) if match else None


def _other_hosts_list(code: str, start: int, host: str | None) -> bool:
    """Whether the mention at `start` reads a literal host's list, and not `host`'s.

    A bare mention, or one through `hostvars[<variable>]`, names no host and reads any list.
    With `host` None the edited host is unknown, so no mention is another host's.
    """
    if host is None:
        return False
    match = _HOSTVARS_LITERAL.search(code, 0, start)
    if not match:
        return False
    return (match.group(1) if match.group(1) is not None else match.group(2)) != host


def changed_fields(before: dict, after: dict) -> dict[str, frozenset[str]] | None:
    """The fields a `containers_list` edit changed, by entry; None when it moves every reader.

    An entry the edit left alone is absent. An added or removed entry moves every reader that
    iterates the list, whatever it reads, and a renamed entry is one of each. A reorder moves
    them too, with no field changed: `scrape_jobs` and `kuma_ingress_monitors` emit in list
    order. A name two entries share is every field as well: `entries` keeps the last of them
    and a `NAMED_ENTRY` filter reads the first, so an edit to the first would go unseen.
    `test_containers_list_names_unique.py` keeps a repeated name off master, so this branch
    is a defence the deployer's range never reaches (#4399).
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


def reader_reads(
    text: str, host: str | None = None
) -> dict[str | Job | None, frozenset[str]] | None:
    """The entry fields a template reads through `containers_list`, by entry; None for all.

    A mention through `hostvars['<other>']`, where `<other>` is a literal host other than
    `host`, reads a list the edit did not change and contributes nothing, whatever its shape.

    The key is the entry a `NAMED_ENTRY` filter looks up by a literal name, the `Job` a
    `NAMED_JOB` filter looks up by a literal job, or None for a read that may land on any
    entry. Every mention inside Jinja code must hand the list straight to
    a filter `FILTER_FIELDS` names, past any `default`. One mention that does not (a loop,
    `selectattr`, an index, a filter returning entries) makes the whole template a reader of
    every field.
    """
    reads: dict[str | Job | None, frozenset[str]] = {}
    for chunk in _JINJA_CODE.findall(_JINJA_COMMENT.sub("", text)):
        for mention in _MENTION.finditer(chunk):
            if _other_hosts_list(chunk, mention.start(), host):
                continue
            name, pos = _filter_after(chunk, mention.end())
            if name not in FILTER_FIELDS:
                return None
            literal = _literal_name(chunk, pos)
            entry: str | Job | None = None
            if literal is not None and name in NAMED_ENTRY:
                entry = literal
            elif literal is not None and name in NAMED_JOB:
                entry = Job(literal)
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
    reads: dict[str | Job | None, frozenset[str]],
    changed: dict[str, frozenset[str]],
    owners: Mapping[str, str | None] | None = None,
) -> bool:
    """Whether a template's `reads` meet the fields an edit `changed`.

    A read keyed by an entry meets only that entry's changes, and an unkeyed read meets any
    entry's. A `Job` read is keyed by its job's entry in `owners`, as `job_owners` returns it
    for the edited document. It stays unkeyed when the edit changed any entry's `metrics`,
    which can move the job to another entry, or when no entry in `owners` declares the job.
    """
    anywhere = frozenset().union(*changed.values())
    owners = {} if "metrics" in anywhere else (owners or {})
    for key, fields in reads.items():
        entry = owners.get(key.name) if isinstance(key, Job) else key
        if fields & (anywhere if entry is None else changed.get(entry, frozenset())):
            return True
    return False


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


def renders_containers_list(text: str, host: str | None = None) -> bool:
    """True when a template mentions `containers_list` inside `{{ }}` or `{% %}`.

    A mention of another literal host's list does not count, as `reader_reads` skips it.

    A mention anywhere else is prose: a `{# #}` header, a `k8s_autodeploy_reason` string, a
    YAML comment. Counting those reached 59 of 62 services on the real tree,
    through `volume-claim`'s 25 callers and the `ingressroute` macro's importers, where the
    templates that read the list number four. `{# #}` is stripped first, because a header
    quotes code.
    """
    code = _JINJA_CODE.findall(_JINJA_COMMENT.sub("", text))
    return any(
        not _other_hosts_list(chunk, mention.start(), host)
        for chunk in code
        for mention in _MENTION.finditer(chunk)
    )


def reader_paths(
    hits: Iterable[str],
    skip_prefixes: tuple[str, ...],
    read: Callable[[str], str | None],
    edit: Edit | None = None,
) -> list[str]:
    """The grep hits whose template reads the fields `edit` changed, in the order given.

    An `edit` whose `changed` is None, or no `edit` at all, is every field of every entry, so
    every hit that renders the list is kept.

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
        host = edit.host if edit is not None else None
        if not renders_containers_list(text, host):
            continue
        if edit is None or edit.changed is None:
            kept.append(hit)
            continue
        reads = reader_reads(text, host)
        if reads is None or reads_changed(reads, edit.changed, edit.owners):
            kept.append(hit)
    return kept
