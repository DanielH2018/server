"""`probe.py b2-longhorn` and `probe.py kuma-drift`: what the estate holds, and what is missing.

`monitors` answers "what is down". `kuma-drift` answers "what is missing", which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up.
"""

import pytest

import probe
import probe_core as core
import probe_storage as storage

#
# Longhorn reports a backup `Completed` once its metadata is written, so "Completed" is not
# evidence the DATA reached B2. These cover the distinction the command exists to make, and
# the credential-handling that keeps it safe to run.

LSF = [
    "backupstore/volumes/aa/bb/pvc-authelia/volume.cfg;120",
    "backupstore/volumes/aa/bb/pvc-authelia/backups/backup_x.cfg;340",
    "backupstore/volumes/aa/bb/pvc-authelia/blocks/1a/2b/deadbeef.blk;2097152",
    "backupstore/volumes/aa/bb/pvc-authelia/blocks/1a/2c/cafebabe.blk;1048576",
    "backupstore/volumes/cc/dd/pvc-bento/blocks/0f/0e/f00d.blk;524288",
]


def test_b2_credentials_travel_in_the_stdin_config_not_argv():
    """argv is visible in `ps`, so the application key must only ever reach curl's stdin.

    The old Docker implementation kept the key out of argv by having `docker exec -e VAR`
    inherit it; curl's `--config -` is the same guard by the route the rest of this file
    already uses for HA and the *arr apps.
    """
    body = storage.b2_authorize_config("keyid123", "appkey456")
    assert 'user = "keyid123:appkey456"' in body
    assert storage.B2_AUTHORIZE_URL in body


def test_b2_list_config_carries_the_token_as_a_header_and_scopes_the_prefix():
    body = storage.b2_list_files_config("https://api.example", "tok", "bid", "longhorn")
    assert 'header = "Authorization: tok"' in body
    assert "prefix=longhorn%2F" in body and "bucketId=bid" in body


def test_b2_longhorn_lines_strips_the_prefix_and_pages():
    """Paths must come back RELATIVE to the prefix, as rclone's lsf produced them.

    B2 returns absolute names (`longhorn/backupstore/...`). Leaving them absolute matches
    none of parse_longhorn_listing's patterns, so a perfectly healthy bucket would report
    "no Longhorn backup objects" — a false data-loss alarm.
    """
    pages = [
        {
            "apiInfo": {
                "storageApi": {"apiUrl": "https://api.example", "bucketId": "b"}
            },
            "authorizationToken": "tok",
            "accountId": "acct",
        },
        {
            "files": [
                {
                    "fileName": "longhorn/backupstore/volumes/aa/bb/pvc-x/blocks/1/2/a.blk",
                    "contentLength": 2097152,
                }
            ],
            "nextFileName": "more",
        },
        {
            "files": [
                {
                    "fileName": "longhorn/backupstore/volumes/aa/bb/pvc-x/volume.cfg",
                    "contentLength": 120,
                }
            ]
        },
    ]
    calls = iter(pages)
    lines = storage.b2_longhorn_lines(
        "k", "s", "bucket", "longhorn", _call=lambda _body: next(calls)
    )
    assert lines == [
        "backupstore/volumes/aa/bb/pvc-x/blocks/1/2/a.blk;2097152",
        "backupstore/volumes/aa/bb/pvc-x/volume.cfg;120",
    ]
    # The whole point: these lines survive the parser that the real command feeds them to.
    vols = storage.parse_longhorn_listing(lines)
    assert vols["pvc-x"]["blocks"] == 1 and vols["pvc-x"]["cfgs"] == 1


def test_b2_longhorn_command_does_not_shell_out_to_docker_or_rclone():
    """The regression this rewrite exists for.

    `probe.py b2-longhorn` shelled out to `docker exec kopia rclone ...` and died with
    FileNotFoundError on both k3s nodes from the day Docker was removed (2026-08-14) —
    while the tests stayed green because they only covered the argv builder and the parser.
    Neither binary exists on these hosts, so naming them here is a dead path by definition.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["stdin"] = kwargs.get("input", "")

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Result()

    real_run = probe.subprocess.run
    probe.subprocess.run = fake_run
    try:
        storage.b2_curl('url = "https://api.example"\n')
    finally:
        probe.subprocess.run = real_run

    assert seen["argv"][0] == "curl"
    assert "docker" not in seen["argv"] and "rclone" not in seen["argv"]
    # The url/credentials reach curl through stdin, so argv stays free of both.
    assert seen["stdin"].startswith("url = ")

    # And no `"docker"` argv literal survives anywhere in this module's executable code.
    # Scans the whole file, not a section split on a comment banner: the B2/Longhorn code
    # is its own module now, so the module boundary carries what the marker used to.
    with open(storage.__file__) as fh:
        source = fh.read()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert '"docker"' not in code


def test_parse_longhorn_listing_separates_data_from_metadata():
    vols = storage.parse_longhorn_listing(LSF)
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["block_bytes"] == 2097152 + 1048576
    assert vols["pvc-authelia"]["cfgs"] == 2
    assert vols["pvc-bento"]["blocks"] == 1


def test_parse_longhorn_listing_ignores_unrelated_and_malformed_lines():
    vols = storage.parse_longhorn_listing(
        ["", "   ", "kopia/p1234.f;99", "backupstore/volumes/aa;10", "no-semicolon"]
    )
    assert vols == {}


def test_format_longhorn_summary_fails_when_a_volume_has_no_data_blocks():
    """Metadata without blocks is the silent-corruption case worth exiting non-zero on."""
    vols = {"pvc-empty": {"blocks": 0, "block_bytes": 0, "cfgs": 3}}
    text, code = storage.format_longhorn_summary(vols)
    assert code == 1
    assert "NO DATA BLOCKS" in text and "pvc-empty" in text


def test_format_longhorn_summary_passes_when_every_volume_has_blocks():
    text, code = storage.format_longhorn_summary(storage.parse_longhorn_listing(LSF))
    assert code == 0
    assert "pvc-authelia" in text and "NO DATA BLOCKS" not in text


def test_format_longhorn_summary_treats_no_objects_as_failure():
    text, code = storage.format_longhorn_summary({})
    assert code == 1 and "no Longhorn backup objects" in text


def test_parse_backup_budget_prices_a_prune_by_directories_not_blocks():
    """A prune's cost is one ListObjects per block directory, so two blocks sharing a
    second-level directory cost less than two that do not."""
    vols = storage.parse_backup_budget(LSF)
    # pvc-authelia: blocks/ + 1a/ + (1a,2b) + (1a,2c) = 4
    assert vols["pvc-authelia"]["prune"] == 4
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["backups"] == 1
    # volume.cfg is not a backup, so it must not inflate the retention count.
    assert vols["pvc-bento"]["backups"] == 0
    assert vols["pvc-bento"]["prune"] == 3


def test_format_backup_budget_flags_a_shard_over_the_daily_cap():
    over = storage.B2_CLASS_C_DAILY_CAP - storage.B2_BUDGET_RESERVE + 1
    vols = {"pvc-big": {"prune": over, "blocks": 9000, "backups": 4}}
    text, code = storage.format_backup_budget(vols, {"pvc-big": "weekly-backup-d2"})
    assert code == 1
    assert "OVER BUDGET" in text and "weekly-backup-d2" in text


def test_stranded_counts_backups_the_current_tier_does_not_own():
    """Stranded means "no job will ever prune this", not "past retain".

    Longhorn's retain counts only a job's OWN backups, so a daily-era backup on a volume that
    has since moved to a weekday shard is pruned by nothing, ever. Until 2026-08-19 this was
    computed as `max(0, backups - retain)`, which under-reported the live cluster by 4.7x — 7
    against a true 33 — on the number an operator reads before deciding what to delete.
    """
    vols = {"pvc-moved": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-moved": {"daily-backup": 4, "weekly-backup-d2": 1}}
    text, _ = storage.format_backup_budget(
        vols, {"pvc-moved": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "4 stranded backup(s)" in text, text


def test_backups_the_current_tier_owns_are_not_stranded_even_past_retain():
    """The owning job prunes them on its next run, so they are queued, not abandoned."""
    vols = {"pvc-busy": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-busy": {"weekly-backup-d2": 5}}
    text, _ = storage.format_backup_budget(
        vols, {"pvc-busy": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "stranded" not in text, text


def test_stranded_falls_back_to_zero_without_ownership_data():
    """No owners map means nothing is PROVEN stranded — never guess high and prompt a delete."""
    vols = {"pvc-x": {"prune": 10, "blocks": 100, "backups": 5}}
    text, _ = storage.format_backup_budget(
        vols, {"pvc-x": "weekly-backup-d2"}, retain=2
    )
    assert "stranded" not in text, text


def test_format_backup_budget_does_not_charge_a_day_for_an_unscheduled_volume():
    """A volume with no recurring job never runs a backup and so never prunes — charging its
    blocks to a shard would read as an over-budget day that cannot actually happen."""
    vols = {"pvc-idle": {"prune": 99999, "blocks": 9000, "backups": 3}}
    text, code = storage.format_backup_budget(vols, {"pvc-idle": "no-backup"})
    assert code == 0
    assert "never pruned" in text and "pvc-idle" in text


SPEND_LOG = [
    (
        1,
        '[pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc-r-9d333575] time="..." '
        'msg="Created snapshot changed blocks: 104 mappings, 104 blocks and 75 new blocks"',
    ),
    (
        2,
        '[pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab-r-b0d3cf84] time="..." '
        'msg="Created snapshot changed blocks: 77 mappings, 77 blocks and 67 new blocks"',
    ),
    (3, 'time="..." msg="Performing delta block backup"'),
]


def test_parse_duration_seconds_accepts_the_documented_forms():
    assert core.parse_duration_seconds("30m") == 1800
    assert core.parse_duration_seconds("6h") == 21600
    assert core.parse_duration_seconds("2d") == 172800
    assert core.parse_duration_seconds("1w") == 604800


def test_parse_duration_seconds_rejects_junk_rather_than_defaulting():
    """A silently-ignored duration would query Loki's one-hour default and report an empty
    window as 'nothing ran', which is the failure this flag exists to prevent."""
    for bad in ("6", "h", "6y", "-2d", "", "6 h"):
        with pytest.raises(SystemExit):
            core.parse_duration_seconds(bad)


def test_parse_backup_spend_counts_delta_blocks_per_volume():
    """`blocks` is the delta Longhorn walks, and it HeadObjects each one — so that count is the
    backup's Class B cost. `new blocks` is what it uploaded, which is Class A and free."""
    vols = storage.parse_backup_spend(SPEND_LOG)
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["blocks"] == 104
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["new_blocks"] == 75
    assert vols["pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab"]["backups"] == 1
    # The unrelated progress line must not be counted as a backup.
    assert len(vols) == 2


def test_parse_backup_spend_keeps_lines_whose_replica_prefix_was_trimmed():
    """Dropping an unattributable line would understate spend, and understating is the failure
    mode that matters — the cap does not care which volume it was."""
    vols = storage.parse_backup_spend(
        [
            (
                1,
                'msg="Created snapshot changed blocks: 9 mappings, 9 blocks and 2 new blocks"',
            )
        ]
    )
    assert vols["unattributed"]["blocks"] == 9


def test_format_backup_spend_totals_and_says_when_the_window_was_empty():
    text = storage.format_backup_spend(storage.parse_backup_spend(SPEND_LOG), "6h")
    assert "backups over 6h: 181 Class B measured" in text
    empty = storage.format_backup_spend({}, "6h")
    assert "no backups logged" in empty and "widen --since" in empty


def test_parse_b2_ledger_totals_per_tool_and_skips_malformed_lines():
    tools = storage.parse_b2_ledger(
        [
            "2026-08-17T12:00:00Z\tdrain\t972\t59\t5\tretain 2",
            "2026-08-17T13:00:00Z\tdrain\t179\t5\t4\tradarr",
            "2026-08-17T14:00:00Z\tb2-budget\t0\t0\t5\t4 pages",
            "not a ledger line",
            "2026-08-17T15:00:00Z\tdrain\tnot\tnumbers\there\t",
        ]
    )
    assert tools["drain"] == {"runs": 2, "class_a": 1151, "class_b": 64, "class_c": 9}
    assert tools["b2-budget"]["class_c"] == 5
    assert "not a ledger line" not in tools


def test_record_b2_spend_never_raises_when_the_ledger_is_unwritable(monkeypatch):
    """A ledger failure must not fail the real work — the accounting is secondary to the
    operation it is accounting for."""
    monkeypatch.setattr(storage, "B2_LEDGER_DIR", "/proc/cannot/create/this")
    storage.record_b2_spend("drain", class_c=5)  # must not raise


def test_record_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "B2_LEDGER_DIR", str(tmp_path))
    storage.record_b2_spend(
        "drain", class_a=100, class_b=59, class_c=5, note="retain 2"
    )
    storage.record_b2_spend("b2-budget", class_c=5)
    tools = storage.read_b2_ledger()
    assert tools["drain"]["class_b"] == 59
    assert tools["b2-budget"]["class_c"] == 5


def test_b2_longhorn_lines_reports_pages_plus_the_authorize_as_class_c():
    """Each page is one b2_list_file_names and the authorize before them is billable too, so a
    two-page listing costs three Class C — the number the ledger needs."""
    pages = [
        {
            "files": [{"fileName": "longhorn/a", "contentLength": 1}],
            "nextFileName": "b",
        },
        {"files": [{"fileName": "longhorn/b", "contentLength": 2}]},
    ]
    calls = []

    def fake(_config):
        if not calls:
            calls.append(1)
            return {
                "apiInfo": {"storageApi": {"apiUrl": "https://api", "bucketId": "bid"}},
                "authorizationToken": "t",
            }
        return pages.pop(0)

    stats = {}
    storage.b2_longhorn_lines("k", "s", "bucket", _call=fake, _stats=stats)
    assert stats == {"class_c": 3, "pages": 2}


def test_format_backup_spend_shows_maintenance_and_never_sums_the_two_windows():
    """Backups span --since; the ledger covers the UTC day. A combined total would match
    neither, so the report must keep them apart."""
    text = storage.format_backup_spend(
        storage.parse_backup_spend(SPEND_LOG),
        "6h",
        ledger={"drain": {"runs": 2, "class_a": 0, "class_b": 64, "class_c": 9}},
    )
    assert "backups over 6h: 181 Class B measured" in text
    assert "drain" in text and "64 Class B" in text
    assert "245" not in text  # 181 + 64 must not appear as a combined figure


def test_format_backup_budget_flags_a_b2_volume_left_on_the_daily_tier():
    """A PVC provisioned from the longhorn StorageClass lands in `default` until a deploy
    reconciles its label, which on B2 means a prune every night against a weekly budget."""
    vols = {"pvc-new": {"prune": 300, "blocks": 200, "backups": 4}}
    text, code = storage.format_backup_budget(vols, {"pvc-new": "default"})
    assert code == 1
    assert "ON THE DAILY TIER AND ON B2" in text and "pvc-new" in text


def test_format_backup_budget_reports_stranded_backups_not_pending_deletes():
    """Stranded backups are abandoned, not queued. Longhorn enforces retain only when the owning
    job runs against a volume still in its groups, counting only its own backups — so a volume
    that moved tier keeps its old backups forever and only the reaper clears them.

    This asserted `backups - retain` until 2026-08-19, which is a different quantity and made
    the check ratify the bug: 11 backups against retain 4 read as 7 stranded, when the answer
    depends entirely on who owns them. Here the d5 job owns 2, so the other 9 are the strays.
    """
    vols = {"pvc-a": {"prune": 100, "blocks": 50, "backups": 11}}
    owners = {"pvc-a": {"daily-backup": 9, "weekly-backup-d5": 2}}
    text, code = storage.format_backup_budget(
        vols, {"pvc-a": "weekly-backup-d5"}, retain=4, owners=owners
    )
    assert code == 0
    assert "9 stranded backup(s)" in text and "reaper" in text


def test_no_cluster_route_carries_the_retired_k8s_suffix(
    fake_resolve, fake_k8s_endpoint
):
    """The `-k8s` suffix retired 2026-08-15 (870723e8), but probe.py kept building it for
    another five hours: every cluster subcommand 404'd against Traefik's no-Host-match while
    the fixtures below asserted the stale name, so CI ratified the break. Assert on the
    hostnames plan() actually asks for, so a reintroduced suffix fails here first."""
    asked = []

    def record(hostname):
        asked.append(hostname)
        return fake_k8s_endpoint(hostname)

    for argv in (
        ["metric", "up"],
        ["targets"],
        ["loki-labels"],
        ["loki-query", '{job="x"}'],
        ["scrutiny"],
    ):
        probe.plan(argv, fake_resolve, record)

    assert asked, "expected plan() to route these subcommands through k8s_endpoint"
    assert not [h for h in asked if h.endswith("-k8s")]


TEMPLATE_SAMPLE = """\
stringData:
  discord.json: |
    {"type": "notification", "name": "Homelab Alerts", "active": true}
  root-disk.json: |
    {"type": "push", "name": "Root Disk", "interval": 60, "push_token": "x"}
  peer-backup.json: |
    {"type": "push", "name": "WG Pi Peer Backup", "interval": 216000, "push_token": "x"}
  grafana.json: |
    {"type": "http", "name": "k3s Grafana", "url": "https://g.example", "interval": 60}
{% if etcd_snapshot_push_token | default('') %}
  etcd.json: |
    {"type": "push", "name": "Off-box etcd Snapshot", "interval": 90000, "push_token": "x"}
{% endif %}
"""


def test_parse_declared_monitors_reads_names_types_and_gating():
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    # Notifications are not monitors and never appear in monitor_status — counting them would
    # make every run report two phantom missing entries.
    assert "Homelab Alerts" not in declared
    assert declared["Root Disk"] == {
        "type": "push",
        "interval": 60,
        "gated": False,
        "gate": None,
    }
    assert declared["k3s Grafana"]["type"] == "http"
    assert declared["Off-box etcd Snapshot"]["gated"] is True
    # The variable is captured, not just the fact of being gated — that name is what lets the
    # caller resolve the secret instead of assuming it is unset.
    assert declared["Off-box etcd Snapshot"]["gate"] == "etcd_snapshot_push_token"


def test_kuma_drift_reports_a_declared_monitor_that_is_not_live():
    # The 2026-08-20 case: the tile is absent from the exporter, not down, so `monitors`
    # reported 81/81 up for a day. Long-uptime Kuma, so PENDING cannot be the explanation.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "k3s Grafana"}
    text, code = probe.format_kuma_drift(declared, live, 86400 * 3)
    assert code == 1
    assert "WG Pi Peer Backup: declared, not live" in text


def test_kuma_drift_calls_a_push_monitor_pending_inside_its_own_interval():
    # Kuma exports a monitor only after it beats, so a restart empties every push series. A
    # monitor whose interval has not elapsed since the restart is not yet due — flagging it
    # would make this check fail after every deploy.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"k3s Grafana"}
    text, code = probe.format_kuma_drift(declared, live, 30)
    assert code == 0
    assert "no beat due yet" in text
    assert "declared, not live" not in text


def test_kuma_drift_treats_every_type_as_pending_after_a_restart():
    # The first live run of this check reported 58 monitors missing 88 seconds into a rollout.
    # Kuma's exporter emits a monitor only after it beats, and that applies to http/port/dns
    # tiles too — restricting the pending rule to push monitors made a routine deploy look like
    # mass drift. The slack covers the exporter's and Prometheus's scrape lag on top.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe.format_kuma_drift(declared, set(), 88)
    assert code == 0
    assert "k3s Grafana: no beat due yet" in text


def test_kuma_drift_fails_loud_when_the_pod_age_is_unreadable():
    # Same rule as `health`'s unreadable restart time: an unknown age must not silently excuse
    # a missing monitor, or the check reports green exactly when it cannot tell.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe.format_kuma_drift(declared, {"k3s Grafana"}, None)
    assert code == 1
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_reports_a_live_monitor_nobody_declared():
    # `kubectl apply` leaves orphaned objects behind, and AutoKuma's on_delete=delete only
    # removes what it still tracks — a monitor whose declaration was dropped can outlive it.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana", "Retired Tile"}
    text, code = probe.format_kuma_drift(declared, live, 86400)
    assert code == 1
    assert "Retired Tile: live, not declared" in text


def test_kuma_drift_skips_a_monitor_whose_gate_is_genuinely_unset():
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": False}
    )
    assert code == 0
    assert "Off-box etcd Snapshot" in text
    assert "genuinely unset" in text


def test_kuma_drift_reports_drift_when_the_gate_is_set_but_the_monitor_is_absent():
    """The 2026-08-22 case, and the reason `gate` exists.

    etcd_snapshot_push_token was set (32 chars, in the rotation registry since 2026-07-04) and
    Off-box etcd Snapshot was not live — and the old check called that correctly skipped. A
    gated monitor that vanishes was invisible twice: absent from the exporter, and excused by
    the drift check written to catch exactly that.
    """
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    # Past the monitor's own 90000s interval, so `pending` cannot absorb it — a gate-set
    # monitor inside its interval is still legitimately pending, not drift.
    text, code = probe.format_kuma_drift(
        declared, live, 86400 * 3, gate_states={"etcd_snapshot_push_token": True}
    )
    assert code == 1
    assert "Off-box etcd Snapshot: declared, not live" in text
    assert "genuinely unset" not in text


def test_kuma_drift_says_so_when_a_gate_cannot_be_read():
    """An unreadable gate and an unset one must not look alike — that equivalence is what let
    the case above stay silent. Unreadable does not fail the exit code (no age key on this
    host is a normal state), but it is named rather than swallowed."""
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": None}
    )
    assert code == 0
    assert "could not be read" in text
    assert "genuinely unset" not in text
