"""The tables `narrow_lib/containers.py` narrows by, held against the filter plugins.

`test_filter_fields_cover_every_key_each_filter_reads` holds `FILTER_FIELDS` against the filter
plugins' own source, so a filter that starts reading a new key fails here rather than
narrowing past a role whose render that key moves. The scan in `_filter_field_scan.py` reports
any use of an entry it cannot resolve to a literal key as opaque: a computed key, iterating the
entry, handing it to a call or a method, returning it from the filter. An opaque use fails
`test_no_filter_in_the_table_reads_a_field_the_scan_cannot_name` unless `VETTED_OPAQUE` names
it with the reason it reads no field the row omits.

`test_every_named_entry_filter_reads_only_the_entry_it_names` holds `NAMED_ENTRY` against the
filters' behaviour: it sets entries beside the target, one valid with other values and one whose
every field is invalid, and asserts the named filter's answer stays put. The valid one, read in
the target's place, must return an answer that differs from the target's, never a raise.
`test_metrics_port_reads_only_the_entry_declaring_its_job` holds `NAMED_JOB` to the same kind
of behaviour, with every entry's `metrics` held (#4396).

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_filter_tables.py
"""

import importlib

import pytest
from ansible.errors import AnsibleFilterError

from deploy_tools.narrow_lib import containers
from lib.repo_paths import REPO

from _filter_field_scan import filter_reads


# ── FILTER_FIELDS against the filter plugins' source ────────────────────────────────────


# A use the scan cannot resolve to a literal field name, vetted as reading no field the row omits.
VETTED_OPAQUE = {
    ("in_service_tier", "found.append()"): (
        "`tier_entries` collects whole entries into the list it returns, and "
        "`in_service_tier` reads each one by the literal key `name`"
    ),
}


def _plugin_reads() -> dict[str, tuple[set[str], set[str]]]:
    reads: dict[str, tuple[set[str], set[str]]] = {}
    for path in sorted((REPO / "ansible" / "filter_plugins").glob("*.py")):
        reads.update(filter_reads(path.read_text()))
    return reads


def _unvetted(name: str, opaque: set[str]) -> set[str]:
    return {use for use in opaque if (name, use) not in VETTED_OPAQUE}


def test_filter_fields_cover_every_key_each_filter_reads():
    reads = _plugin_reads()
    # Non-vacuity: the scan must find the filters the table names, entry_port among them.
    assert "entry_port" in reads and set(containers.FILTER_FIELDS) <= set(reads)
    missing = {
        name: sorted(reads[name][0] - fields)
        for name, fields in containers.FILTER_FIELDS.items()
        if reads[name][0] - fields
    }
    assert not missing, f"FILTER_FIELDS omits keys these filters read: {missing}"


def test_no_filter_in_the_table_reads_a_field_the_scan_cannot_name():
    """A row whose filter iterates or forwards the entry would narrow past a moved render."""
    reads = _plugin_reads()
    # Non-vacuity: the table leaves `filter_by_platform` out because it returns whole entries,
    # and the scan must reach the same verdict from the source.
    assert any(use.startswith("return ") for use in reads["filter_by_platform"][1])
    opaque = {
        name: sorted(_unvetted(name, reads[name][1]))
        for name in containers.FILTER_FIELDS
        if _unvetted(name, reads[name][1])
    }
    assert not opaque, (
        "these filters read fields no constant names; drop their FILTER_FIELDS row so their "
        f"readers read every field, or vet the use in VETTED_OPAQUE: {opaque}"
    )
    # The vetted uses must still exist, or the exemption outlived its subject.
    assert all(use in reads[name][1] for name, use in VETTED_OPAQUE)


_ENTRY_PORT = (
    "def entry_port(containers_list, name):\n"
    "    return _lookup(containers_list, name)['port']\n"
    "def _lookup(containers_list, name):\n"
    "    return [e for e in containers_list if {probe}][0]\n"
    "class FilterModule:\n"
    "    def filters(self):\n"
    "        return {{'entry_port': entry_port}}\n"
)


def test_the_scan_flags_a_filter_reading_a_key_the_table_omits():
    keys, _ = filter_reads(_ENTRY_PORT.format(probe="e.get('hostname')"))["entry_port"]
    assert keys - containers.FILTER_FIELDS["entry_port"] == {"hostname"}


@pytest.mark.parametrize(
    ("probe", "use"),
    [
        ("any(e.values())", ".values()"),
        ("e.get(name)", ".get(<computed>)"),
        ("render(**e)", "**"),
        ("getattr(e, name)", "getattr()"),
        ("e[name]", "e[name]"),
        ("any(k for k in e)", "for over e"),
        ("json.dumps(e)", "json.dumps()"),
        ("dict(e)", "dict()"),
    ],
)
def test_the_scan_flags_a_filter_that_iterates_or_forwards_the_entry(probe, use):
    _, opaque = filter_reads(_ENTRY_PORT.format(probe=probe))["entry_port"]
    assert use in opaque


# The entry reaches a helper as a parameter here, and the helper has a local of its own. That is
# the shape the comprehension harness above cannot exercise: there `e` is a loop variable.
_HELPER = (
    "def entry_port(containers_list, name):\n"
    "    for e in containers_list:\n"
    "        if e.get('name') == name:\n"
    "            return _read(e, name)\n"
    "def _read(entry, key):\n"
    "    out = {{}}\n"
    "    {probe}\n"
    "    return out['port']\n"
    "class FilterModule:\n"
    "    def filters(self):\n"
    "        return {{'entry_port': entry_port}}\n"
)


@pytest.mark.parametrize(
    ("probe", "use"),
    [
        ("out['port'] = entry[key]", "entry[key]"),
        ("for k in entry: out[k] = 1", "for over entry"),
        ("out = dict(entry)", "dict()"),
        ("out.update(entry)", "out.update()"),
        ("out['port'] = ', '.join(entry)", "', '.join()"),
    ],
    ids=["computed-key", "iterates", "walks", "attribute-call", "constant-receiver"],
)
def test_a_helper_reading_an_entry_it_was_handed_by_a_computed_route_is_flagged(
    probe, use
):
    _, opaque = filter_reads(_HELPER.format(probe=probe))["entry_port"]
    assert use in opaque


@pytest.mark.parametrize(
    "probe",
    ["out['port'] = entry['port']", "out['port'] = entry.get('port', 0)", "pass"],
)
def test_a_helper_reading_an_entry_by_literal_keys_is_clean(probe):
    _, opaque = filter_reads(_HELPER.format(probe=probe))["entry_port"]
    assert opaque == set()


def test_the_comprehension_harness_reading_a_literal_key_is_clean():
    """Without this the probes above could pass because the harness flags everything."""
    _, opaque = filter_reads(_ENTRY_PORT.format(probe="e.get('name') == name"))[
        "entry_port"
    ]
    assert opaque == set()


def test_a_filter_returning_a_whole_entry_is_flagged():
    """The template then reads whatever it likes from it, as from `selectattr`."""
    source = _HELPER.replace("return _read(e, name)", "return e")
    _, opaque = filter_reads(source.format(probe="pass"))["entry_port"]
    assert "return e" in opaque


# ── NAMED_ENTRY against the filters' behaviour ─────────────────────────────────────────


# Each named filter's arguments after the list and the name.
_NAMED_ARGS = {
    "entry_port": (),
    "homepage_href": ("example.com",),
    "homepage_widget_url": ("media",),
    "in_service_tier": ("home-critical",),
    "metrics_port": (),
    "tier_priority_class": (),
}
_TARGET = {
    "name": "target",
    "port": 8080,
    "hostname": "target",
    "homepage_widget": True,
    "tier": "home-edge",
    "metrics": [{"job": "target", "port": 9100}],
}
# Entries beside the target: one valid entry that answers differently from it, one whose
# every field would fail a filter that read it, and one that is no entry at all.
_NEIGHBOURS = (
    {
        "name": "other",
        "port": 1,
        "hostname": "other",
        "homepage_widget": True,
        "tier": "home-automation",
        "metrics": [{"job": "target", "port": 1}],
    },
    {
        "name": "junk",
        "port": "x",
        "hostname": None,
        "homepage_widget": False,
        "tier": "bogus",
        "metrics": "bogus",
    },
    "not-an-entry",
)


def _filters() -> dict:
    found = {}
    for path in sorted((REPO / "ansible" / "filter_plugins").glob("*.py")):
        found.update(importlib.import_module(path.stem).FilterModule().filters())
    return found


def _outcome(filter_, containers_list: list, args: tuple) -> tuple:
    try:
        return ("returned", filter_(containers_list, "target", *args))
    # A FilterModule refusal is an AnsibleFilterError, which no ValueError subclasses (#4480).
    except (KeyError, TypeError, ValueError, AnsibleFilterError) as exc:
        return ("raised", type(exc).__name__, str(exc))


def _reads_only_the_named_entry(filter_, args: tuple) -> bool:
    """Whether the `_NEIGHBOURS` beside `target` leave what `filter_` answers unchanged.

    The target is tried whole and without each field, so a filter that falls back to another
    entry when its own lacks a field is caught too.
    """
    variants = [_TARGET] + [
        {k: v for k, v in _TARGET.items() if k != key}
        for key in _TARGET
        if key != "name"
    ]
    return all(
        _outcome(filter_, [neighbour, target, neighbour], args)
        == _outcome(filter_, [target], args)
        for target in variants
        for neighbour in _NEIGHBOURS
    )


def test_every_named_entry_filter_reads_only_the_entry_it_names():
    # Non-vacuity: the table must name the lookups the real templates call most.
    assert {"entry_port", "tier_priority_class"} <= containers.NAMED_ENTRY
    assert containers.NAMED_ENTRY <= set(containers.FILTER_FIELDS)
    # The target and the valid neighbour, each read as the entry the filter names, must both
    # be answered, and answered differently. Otherwise a filter that read the neighbour would
    # pass: a `tier` sharing the target's PriorityClass hid exactly that from
    # `tier_priority_class`. A raise is no answer here, so it cannot count as a different one:
    # a filter that skips entries it cannot read, as it skips `junk`, would then skip this
    # neighbour too, and nothing beside the target would tell it apart.
    filters = _filters()
    as_target = {**_NEIGHBOURS[0], "name": "target"}
    answers = {
        n: (
            _outcome(filters[n], [_TARGET], _NAMED_ARGS[n]),
            _outcome(filters[n], [as_target], _NAMED_ARGS[n]),
        )
        for n in containers.NAMED_ENTRY
    }
    unanswered = {
        n: (target, other)
        for n, (target, other) in answers.items()
        if "raised" in (target[0], other[0])
    }
    assert not unanswered, (
        f"target or valid neighbour raised, so nothing compares: {unanswered}"
    )
    alike = sorted(n for n, (target, other) in answers.items() if target == other)
    assert not alike, f"the valid neighbour answers as the target does for: {alike}"
    walkers = sorted(
        n
        for n in containers.NAMED_ENTRY
        if not _reads_only_the_named_entry(filters[n], _NAMED_ARGS[n])
    )
    assert not walkers, (
        f"these NAMED_ENTRY filters read entries other than the one they name: {walkers}; "
        "drop them from NAMED_ENTRY so their readers stay keyed by no entry"
    )


# The entry declaring the job `target`, with and without a port of the item's own.
_JOB_OWNERS = (
    {"name": "owner", "port": 8080, "metrics": [{"job": "target"}]},
    {"name": "owner", "port": 8080, "metrics": [{"job": "target", "port": 9100}]},
)
# Entries beside the owner that declare no `target` job, varying every field but `metrics`.
_NON_OWNERS = (
    {
        "name": "a",
        "port": 60000,
        "tier": "home-edge",
        "metrics": [{"job": "b", "port": 2}],
    },
    {"name": "b", "port": "x", "hostname": None},
    "not-an-entry",
)


def _answers_from_the_job_owner_alone(filter_) -> bool:
    return all(
        _outcome(filter_, [other, owner, other], ()) == _outcome(filter_, [owner], ())
        for owner in _JOB_OWNERS
        for other in _NON_OWNERS
    )


def test_metrics_port_reads_only_the_entry_declaring_its_job():
    assert containers.NAMED_JOB == {"metrics_port"}
    assert _answers_from_the_job_owner_alone(_filters()["metrics_port"])
    # Why a `metrics` edit unkeys the read: an earlier entry declaring the job moves it.
    earlier = {"name": "early", "port": 1, "metrics": [{"job": "target"}]}
    owner = _JOB_OWNERS[0]
    assert _outcome(_filters()["metrics_port"], [earlier, owner], ()) != _outcome(
        _filters()["metrics_port"], [owner], ()
    )


def test_a_job_filter_reading_another_entrys_port_is_flagged():
    metrics_port = _filters()["metrics_port"]

    def with_the_widest_port(containers_list, job):
        ports = [e["port"] for e in containers_list if isinstance(e, dict)]
        return max(
            [metrics_port(containers_list, job)]
            + [p for p in ports if isinstance(p, int)]
        )

    assert not _answers_from_the_job_owner_alone(with_the_widest_port)


@pytest.mark.parametrize("name", ["metrics_port", "in_service_tier"])
def test_a_filter_that_reads_entries_beside_the_one_it_names_is_flagged(name: str):
    """The two `NAMED_ENTRY` leaves out: a job any entry may declare, a tier check over all."""
    assert name not in containers.NAMED_ENTRY
    assert not _reads_only_the_named_entry(_filters()[name], _NAMED_ARGS[name])


def test_a_filter_answering_from_a_valid_neighbour_is_flagged():
    """Only the valid neighbour can catch this one, so the guard must keep consulting it.

    It raises wherever `tier_priority_class` raises, so the target-without-a-field variants
    match, and it skips `junk` and the non-entry, so they match too. Both flagged filters above
    also fail through `junk`, which would hide the walk dropping the valid neighbour.
    """
    service_tier = importlib.import_module("service_tier")

    def from_a_neighbour(containers_list, name):
        answer = service_tier.tier_priority_class(containers_list, name)
        for entry in containers_list:
            if (
                isinstance(entry, dict)
                and entry.get("name") != name
                and entry.get("tier") in service_tier.SERVICE_TIERS
            ):
                return service_tier.SERVICE_TIERS[entry["tier"]]
        return answer

    assert not _reads_only_the_named_entry(from_a_neighbour, ())
