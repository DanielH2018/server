"""A `containers_list` entry edit reaches the readers of the fields it changed, not every reader.

Before #4340 any entry edit reached every role whose templates read the list. On the range of
PR #4310 six entry edits reached 23 readers that way, though most of them read one field the
edits never touched. `narrow_lib/containers.py` now maps an edit to its changed fields and a
reader to the fields its filters read, and keeps a reader only where the two meet. A reader it
cannot name the fields of (a loop, `selectattr`, a filter missing from `FILTER_FIELDS`) still
counts as reading every field. A filter that looks up one entry by a literal name keys its read on
that entry too (#4388), so an edit to radarr's `port` skips a reader of sonarr's. A
`metrics_port` read by a literal job is keyed on the entry declaring that job, while the edit
leaves every entry's `metrics` alone (#4396). A read through `hostvars['<host>']` with a literal
host is keyed on that host's list, so an edit to another host's `host_vars` skips it (#4422).

`test_narrow_filter_tables.py` holds the tables this relies on, `FILTER_FIELDS`, `NAMED_ENTRY`
and `NAMED_JOB`, against the filter plugins.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_containers_fields.py
"""

from pathlib import Path

import pytest

from deploy_tools.narrow_lib import broad as narrow_broad
from deploy_tools.narrow_lib import containers
from lib.repo_paths import REPO

from _narrow_fixtures import DECLARED, HOST_VARS, Tree, _refs, build_tree

BOX = "ansible/inventory/host_vars/daniel-box.yml"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    # prowlarr reads one entry's port, lidarr walks the whole list.
    t.write(
        "ansible/roles/k8s/prowlarr/templates/configmap.yaml.j2",
        "url: http://sonarr:{{ containers_list | entry_port('sonarr') }}\n",
    )
    t.write(
        "ansible/roles/k8s/lidarr/templates/configmap.yaml.j2",
        "{% for e in containers_list %}{{ e.name }}{% endfor %}\n",
    )
    t.commit("two readers of the list")
    return t


def test_an_edit_to_a_field_no_filter_reads_skips_the_filter_reader(tree: Tree):
    tree.write(BOX, HOST_VARS.replace("image: sonarr:1", "image: sonarr:2"))
    # sonarr is the entry's own tag; lidarr's loop reads every field; prowlarr reads `port`.
    assert tree.narrow(*_refs(tree)) == {"sonarr", "lidarr"}


def test_an_edit_to_a_field_the_filter_reads_reaches_the_filter_reader(tree: Tree):
    tree.write(
        BOX, HOST_VARS.replace("image: sonarr:1", "image: sonarr:1\n    port: 8989")
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "lidarr", "prowlarr"}


def test_an_edit_to_another_entrys_field_skips_a_reader_of_one_entry(tree: Tree):
    """prowlarr reads only sonarr's `port`, so a `port` edit to radarr cannot move it."""
    radarr = "  - name: radarr\n    platform: k8s\n"
    tree.write(BOX, HOST_VARS.replace(radarr, radarr + "    port: 7878\n"))
    assert tree.narrow(*_refs(tree)) == {"radarr", "lidarr"}


def test_a_lookup_by_a_variable_name_reaches_every_entrys_edit(tree: Tree):
    tree.write(
        "ansible/roles/k8s/bazarr/templates/configmap.yaml.j2",
        "port: {{ containers_list | entry_port(peer) }}\n",
    )
    tree.commit("a reader whose entry is a variable")
    radarr = "  - name: radarr\n    platform: k8s\n"
    tree.write(BOX, HOST_VARS.replace(radarr, radarr + "    port: 7878\n"))
    assert tree.narrow(*_refs(tree)) == {"radarr", "lidarr", "bazarr"}


_RADARR = "  - name: radarr\n    platform: k8s\n"


def _bazarr_reads_radarrs_job(tree: Tree) -> str:
    """Commit a `radarr-x` job on radarr and a bazarr reader of it; return the host_vars."""
    with_job = HOST_VARS.replace(
        _RADARR, _RADARR + "    metrics:\n      - job: radarr-x\n"
    )
    tree.write(BOX, with_job)
    tree.write(
        "ansible/roles/k8s/bazarr/templates/service.yaml.j2",
        "port: {{ containers_list | metrics_port('radarr-x') }}\n",
    )
    tree.commit("a reader of radarr's metrics job")
    return with_job


def test_a_port_edit_skips_a_reader_of_another_entrys_metrics_job(tree: Tree):
    """bazarr reads the job radarr declares, so sonarr's `port` cannot move it (#4396)."""
    with_job = _bazarr_reads_radarrs_job(tree)
    sonarr = "    image: sonarr:1\n"
    tree.write(BOX, with_job.replace(sonarr, sonarr + "    port: 8989\n"))
    assert tree.narrow(*_refs(tree)) == {"sonarr", "lidarr", "prowlarr"}


def test_a_port_edit_to_the_jobs_entry_reaches_its_metrics_port_reader(tree: Tree):
    with_job = _bazarr_reads_radarrs_job(tree)
    tree.write(BOX, with_job.replace(_RADARR, _RADARR + "    port: 7878\n"))
    assert tree.narrow(*_refs(tree)) == {"radarr", "lidarr", "bazarr"}


def test_an_added_entry_reaches_every_reader(tree: Tree):
    tree.write(BOX, HOST_VARS + "  - name: readarr\n    platform: k8s\n")
    assert tree.narrow(*_refs(tree)) == {"readarr", "lidarr", "prowlarr"}


@pytest.mark.parametrize(
    "line",
    [
        "{{ containers_list | metrics_port('crowdsec') }}",
        "{{ hostvars['daniel-pi'].containers_list | entry_port('alloy') }}",
        "{{ (containers_list | homepage_href('docs', domain)) }}",
    ],
)
def test_a_filter_reader_has_the_filters_fields_is_clean(line: str):
    assert containers.reader_reads(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "{% for e in containers_list %}{{ e.port }}{% endfor %}",
        "{{ containers_list | selectattr('port', 'defined') | list }}",
        "{% for s in containers_list | default([]) | filter_by_platform('k8s') %}",
        "{{ containers_list }}",
        "{{ containers_list | entry_port('a') }} {{ containers_list[0].hostname }}",
    ],
    ids=["loop", "selectattr", "entries-returned", "bare", "one-bare-use-among-two"],
)
def test_a_reader_whose_fields_cannot_be_named_reads_every_field_is_flagged(line: str):
    assert containers.reader_reads(line) is None


@pytest.mark.parametrize(
    ("line", "entry"),
    [
        ("{{ containers_list | entry_port('uptime-kuma') }}", "uptime-kuma"),
        ('{{ containers_list | homepage_href( "docs", domain) }}', "docs"),
        (
            "{{ containers_list | default([]) | tier_priority_class('pihole') }}",
            "pihole",
        ),
    ],
)
def test_a_named_lookup_by_a_literal_is_keyed_by_that_entry_is_clean(line, entry):
    reads = containers.reader_reads(line)
    assert reads is not None and set(reads) == {entry}


@pytest.mark.parametrize(
    "line",
    [
        "{{ containers_list | entry_port(t.service) }}",
        "{{ containers_list | entry_port('uptime-' ~ suffix) }}",
        "{{ containers_list | entry_port('a' | lower) }}",
        "{{ containers_list | metrics_port(job) }}",
    ],
    ids=["variable", "concatenated", "filtered", "job-variable"],
)
def test_a_lookup_by_anything_but_a_literal_entry_name_is_keyed_by_no_entry_is_flagged(
    line,
):
    reads = containers.reader_reads(line)
    assert reads is not None and set(reads) == {None}


def test_a_metrics_port_read_by_a_literal_job_is_keyed_by_the_job_is_clean():
    reads = containers.reader_reads("{{ containers_list | metrics_port('crowdsec') }}")
    assert reads == {
        containers.Job("crowdsec"): containers.FILTER_FIELDS["metrics_port"]
    }


_JOB_READ: dict[str | containers.Job | None, frozenset[str]] = {
    containers.Job("x"): containers.FILTER_FIELDS["metrics_port"]
}


def test_a_job_read_meets_only_its_owners_edit_is_clean():
    owners = {"x": "a"}
    assert not containers.reads_changed(_JOB_READ, {"b": frozenset({"port"})}, owners)
    assert containers.reads_changed(_JOB_READ, {"a": frozenset({"port"})}, owners)


@pytest.mark.parametrize(
    ("changed", "owners"),
    [
        ({"b": frozenset({"metrics"})}, {"x": "a"}),
        ({"b": frozenset({"port"})}, {}),
        ({"b": frozenset({"port"})}, {"x": None}),
    ],
    ids=["metrics-moved", "no-owner", "unnamed-owner"],
)
def test_a_job_read_meets_any_entrys_edit_when_its_owner_is_unknown_is_flagged(
    changed, owners
):
    assert containers.reads_changed(_JOB_READ, changed, owners)


def test_job_owners_names_the_first_entry_declaring_each_job():
    doc = {
        "containers_list": [
            {"name": "a", "metrics": [{"job": "x"}]},
            {"metrics": [{"job": "y"}]},
            {"name": "b", "metrics": [{"job": "x"}, {"job": "y"}, {"job": "z"}]},
            {"name": "c", "metrics": "bogus"},
        ]
    }
    assert containers.job_owners(doc) == {"x": "a", "y": None, "z": "b"}


def test_changed_fields_names_the_keys_an_edit_moved_by_entry():
    before = {"containers_list": [{"name": "a", "port": 1}, {"name": "b", "port": 1}]}
    after = {
        "containers_list": [
            {"name": "a", "port": 2, "tier": "t"},
            {"name": "b", "port": 1},
        ]
    }
    assert containers.changed_fields(before, after) == {"a": {"port", "tier"}}


def test_a_repeated_name_changes_every_field():
    """`entries` keeps the second `a`, and a named filter reads the first, which moved."""
    before = {"containers_list": [{"name": "a", "port": 1}, {"name": "a", "port": 1}]}
    after = {"containers_list": [{"name": "a", "port": 2}, {"name": "a", "port": 1}]}
    assert containers.changed_fields(before, after) is None


def test_a_reorder_reaches_every_reader(tree: Tree):
    """No field moves, but `scrape_jobs` and `kuma_ingress_monitors` render in list order."""
    radarr = "  - name: radarr\n    platform: k8s\n"
    tree.write(
        BOX,
        HOST_VARS.replace(radarr, "").replace(
            "containers_list:\n", "containers_list:\n" + radarr
        ),
    )
    assert tree.narrow(*_refs(tree)) == {"lidarr", "prowlarr"}


def test_a_reorder_changes_every_field():
    before = {"containers_list": [{"name": "a"}, {"name": "b"}]}
    after = {"containers_list": [{"name": "b"}, {"name": "a"}]}
    assert containers.changed_fields(before, after) is None


def test_a_removed_entry_changes_every_field():
    before = {"containers_list": [{"name": "a"}, {"name": "b"}]}
    assert (
        containers.changed_fields(before, {"containers_list": [{"name": "a"}]}) is None
    )


def test_the_real_tree_names_filter_readers():
    """The narrowing finds its readers by pattern, so it must still see named ones."""
    reader = (
        REPO
        / "ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2"
    )
    assert containers.reader_reads(Path(reader).read_text()) == {
        "uptime-kuma": {"name", "port"}
    }


@pytest.mark.parametrize(
    "template",
    [
        "artifacts/templates/configmap.yaml.j2",
        "monitor-bridge/templates/env-secret.yaml.j2",
        "netpol-baseline/templates/networkpolicy-callers.yaml.j2",
        "pihole/templates/config/pihole-dnsmasq.conf.j2",
        "uptime-kuma/templates/deployment.yaml.j2",
    ],
)
def test_a_former_list_loop_reads_named_fields_is_clean(template: str):
    """These five looped the list, so every entry edit reached their roles (#4395)."""
    reads = containers.reader_reads((REPO / "ansible/roles/k8s" / template).read_text())
    assert reads is not None
    assert not any("depends_on" in fields for fields in reads.values())


PI = "ansible/inventory/host_vars/daniel-pi.yml"
_PI_VARS = "containers_list:\n  - name: alloy\n    port: 12345\n"


def _host_keyed_readers(tree: Tree, quote: str = "'") -> None:
    """Commit a Pi list, one reader keyed on each host and one keyed on a variable host.

    bazarr reads the Pi's list as monitor-bridge does, jellyfin reads daniel-box's, and
    radarr reads `hostvars[peer]`, whose host no literal names, and readarr reads the list
    bare, which names no host either (#4422).
    """
    tree.write(PI, _PI_VARS)
    for role, host in (("bazarr", "daniel-pi"), ("jellyfin", "daniel-box")):
        tree.write(
            f"ansible/roles/k8s/{role}/templates/secret.yaml.j2",
            f"p: {{{{ hostvars[{quote}{host}{quote}].containers_list"
            " | published_ports | join(',') }}\n",
        )
    tree.write(
        "ansible/roles/k8s/radarr/templates/secret.yaml.j2",
        "p: {{ hostvars[peer].containers_list | published_ports | join(',') }}\n",
    )
    tree.write(
        "ansible/roles/k8s/readarr/templates/secret.yaml.j2",
        "p: {{ containers_list | published_ports | join(',') }}\n",
    )
    tree.commit("readers keyed on a host")


def _narrow(tree: Tree) -> set[str]:
    """`Tree.narrow` over a fleet wide enough that seven tags stay under its ceiling."""
    old, new = _refs(tree)
    spare = {"monitor-bridge", "readarr"} | {f"spare-{n}" for n in range(6)}
    return narrow_broad.narrow(
        old, new, cwd=tree.root, declared=DECLARED | spare, callers={}
    )


@pytest.mark.parametrize("quote", ["'", '"'], ids=["single", "double"])
def test_a_port_edit_to_daniel_box_skips_a_reader_of_the_pis_list(
    tree: Tree, quote: str
):
    """bazarr renders only daniel-pi's list, so a daniel-box `port` edit cannot move it."""
    _host_keyed_readers(tree, quote)
    sonarr = "    image: sonarr:1\n"
    tree.write(BOX, HOST_VARS.replace(sonarr, sonarr + "    port: 8989\n"))
    # radarr's host is a variable and readarr's and lidarr's reads are bare: all unkeyed.
    assert _narrow(tree) == {
        "sonarr",
        "prowlarr",
        "jellyfin",
        "radarr",
        "readarr",
        "lidarr",
    }


def test_a_port_edit_to_daniel_pi_skips_a_reader_of_daniel_boxs_list(tree: Tree):
    _host_keyed_readers(tree)
    tree.write(PI, _PI_VARS.replace("12345", "12346"))
    # prowlarr reads sonarr's port by name, which daniel-pi's list does not declare.
    assert _narrow(tree) == {"alloy", "bazarr", "radarr", "readarr", "lidarr"}


def test_a_port_edit_to_daniel_box_skips_monitor_bridge(tree: Tree):
    """monitor-bridge's `port` reads are uptime-kuma's entry and the Pi's list (#4422)."""
    secret = "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2"
    tree.write(secret, (REPO / secret).read_text())
    tree.write(PI, _PI_VARS)
    tree.commit("monitor-bridge as it stands")
    sonarr = "    image: sonarr:1\n"
    tree.write(BOX, HOST_VARS.replace(sonarr, sonarr + "    port: 8989\n"))
    assert "monitor-bridge" not in _narrow(tree)


def test_an_added_daniel_box_entry_skips_a_reader_of_the_pis_list(tree: Tree):
    """An added entry changes every field, and still only daniel-box's list (#4422)."""
    _host_keyed_readers(tree)
    added = "  - name: whisparr\n    image: whisparr:1\n"
    tree.write(BOX, HOST_VARS + added)
    assert "bazarr" not in _narrow(tree)


@pytest.mark.parametrize(
    "path, host",
    [
        ("ansible/inventory/host_vars/daniel-pi.yml", "daniel-pi"),
        ("ansible/inventory/host_vars/daniel-box/containers.yml", "daniel-box"),
        ("ansible/inventory/group_vars/all.yml", None),
    ],
)
def test_edited_host_names_the_host_vars_owner_only(path: str, host: str | None):
    assert containers.edited_host(path) == host


# The five readers of every field D2's baseline counted at 70591c307, each in its shape there.
# 8e79e95d6 moved the live five onto named filters; the pair keeps a reader of every field
# present, because a template that adds one meets the same `reader_paths` rule.
WHOLE_ENTRY_READERS = {
    "pihole": "{% for s in hostvars[k8s_primary_node].containers_list | default([])"
    " | filter_by_platform('k8s') %}{{ s.hostname }}{% endfor %}\n",
    "uptime-kuma": "{% for e in hostvars[k8s_primary_node].containers_list"
    " | selectattr('hostname', 'defined') %}{{ e.hostname }}{% endfor %}\n",
    "monitor-bridge": "{% set p = containers_list | selectattr('port', 'defined')"
    " | list %}\n",
    "artifacts": "{{ containers_list | map(attribute='name') | list }}\n",
    "netpol-baseline": "{% for e in containers_list %}{{ e.name }}{% endfor %}\n",
}


def _narrow_with_whole_entry_readers(tmp_path, replacement: str) -> set[str]:
    """Commit the five readers, then the sonarr edit `replacement` makes; narrow it."""
    t = build_tree(tmp_path)
    for role, text in WHOLE_ENTRY_READERS.items():
        t.write(f"ansible/roles/k8s/{role}/templates/reader.yaml.j2", text)
    t.commit("five readers of every field")
    t.write(BOX, HOST_VARS.replace("image: sonarr:1", replacement))
    # Bystander tags, as on the real fleet's 59: six of eleven would trip the ceiling `narrow`
    # puts on a tag list covering most of the fleet.
    declared = DECLARED | set(WHOLE_ENTRY_READERS) | {f"other-{n}" for n in range(6)}
    return narrow_broad.narrow(*_refs(t), cwd=t.root, declared=declared, callers={})


def test_an_owner_only_edit_maps_to_the_entrys_own_tag_alone_is_clean(tmp_path):
    """`owner` is a deploy-mechanics field, so no template reader moves (D2.1a)."""
    edited = _narrow_with_whole_entry_readers(
        tmp_path, "image: sonarr:1\n    owner: flux"
    )
    assert edited == {"sonarr"}


def test_a_hostname_edit_reaches_the_five_whole_entry_readers_is_flagged(tmp_path):
    edited = _narrow_with_whole_entry_readers(
        tmp_path, "image: sonarr:1\n    hostname: sonarr"
    )
    assert edited == {"sonarr"} | set(WHOLE_ENTRY_READERS)
