"""`probe.py b2-longhorn` and `probe.py b2-budget`: what the estate holds, and what it costs.

Longhorn reports a backup `Completed` once its metadata is written, so "Completed" is not
evidence the DATA reached B2. These cover the distinction the command exists to make, the
credential-handling that keeps it safe to run, and the Class C budget projection.
"""

import probe
import probe_longhorn as longhorn

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
    body = longhorn.b2_authorize_config("keyid123", "appkey456")
    assert 'user = "keyid123:appkey456"' in body
    assert longhorn.B2_AUTHORIZE_URL in body


def test_b2_list_config_carries_the_token_as_a_header_and_scopes_the_prefix():
    body = longhorn.b2_list_files_config(
        "https://api.example", "tok", "bid", "longhorn"
    )
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
    lines = longhorn.b2_longhorn_lines(
        "k", "s", "bucket", "longhorn", _call=lambda _body: next(calls)
    )
    assert lines == [
        "backupstore/volumes/aa/bb/pvc-x/blocks/1/2/a.blk;2097152",
        "backupstore/volumes/aa/bb/pvc-x/volume.cfg;120",
    ]
    # The whole point: these lines survive the parser that the real command feeds them to.
    vols = longhorn.parse_longhorn_listing(lines)
    assert vols["pvc-x"]["blocks"] == 1 and vols["pvc-x"]["cfgs"] == 1


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
    longhorn.b2_longhorn_lines("k", "s", "bucket", _call=fake, _stats=stats)
    assert stats == {"class_c": 3, "pages": 2}


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
        longhorn.b2_curl('url = "https://api.example"\n')
    finally:
        probe.subprocess.run = real_run

    assert seen["argv"][0] == "curl"
    assert "docker" not in seen["argv"] and "rclone" not in seen["argv"]
    # The url/credentials reach curl through stdin, so argv stays free of both.
    assert seen["stdin"].startswith("url = ")

    # And no `"docker"` argv literal survives anywhere in this module's executable code.
    # Scans the whole file, not a section split on a comment banner: the B2/Longhorn code
    # is its own module now, so the module boundary carries what the marker used to.
    with open(longhorn.__file__) as fh:
        source = fh.read()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert '"docker"' not in code


def test_parse_longhorn_listing_separates_data_from_metadata():
    vols = longhorn.parse_longhorn_listing(LSF)
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["block_bytes"] == 2097152 + 1048576
    assert vols["pvc-authelia"]["cfgs"] == 2
    assert vols["pvc-bento"]["blocks"] == 1


def test_parse_longhorn_listing_ignores_unrelated_and_malformed_lines():
    vols = longhorn.parse_longhorn_listing(
        ["", "   ", "kopia/p1234.f;99", "backupstore/volumes/aa;10", "no-semicolon"]
    )
    assert vols == {}


def test_format_longhorn_summary_fails_when_a_volume_has_no_data_blocks():
    """Metadata without blocks is the silent-corruption case worth exiting non-zero on."""
    vols = {"pvc-empty": {"blocks": 0, "block_bytes": 0, "cfgs": 3}}
    text, code = longhorn.format_longhorn_summary(vols)
    assert code == 1
    assert "NO DATA BLOCKS" in text and "pvc-empty" in text


def test_format_longhorn_summary_passes_when_every_volume_has_blocks():
    text, code = longhorn.format_longhorn_summary(longhorn.parse_longhorn_listing(LSF))
    assert code == 0
    assert "pvc-authelia" in text and "NO DATA BLOCKS" not in text


def test_format_longhorn_summary_treats_no_objects_as_failure():
    text, code = longhorn.format_longhorn_summary({})
    assert code == 1 and "no Longhorn backup objects" in text


def test_parse_backup_budget_prices_a_prune_by_directories_not_blocks():
    """A prune's cost is one ListObjects per block directory, so two blocks sharing a
    second-level directory cost less than two that do not."""
    vols = longhorn.parse_backup_budget(LSF)
    # pvc-authelia: blocks/ + 1a/ + (1a,2b) + (1a,2c) = 4
    assert vols["pvc-authelia"]["prune"] == 4
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["backups"] == 1
    # volume.cfg is not a backup, so it must not inflate the retention count.
    assert vols["pvc-bento"]["backups"] == 0
    assert vols["pvc-bento"]["prune"] == 3


def test_format_backup_budget_flags_a_shard_over_the_daily_cap():
    over = longhorn.B2_CLASS_C_DAILY_CAP - longhorn.B2_BUDGET_RESERVE + 1
    vols = {"pvc-big": {"prune": over, "blocks": 9000, "backups": 4}}
    text, code = longhorn.format_backup_budget(vols, {"pvc-big": "weekly-backup-d2"})
    assert code == 1
    assert "OVER BUDGET" in text and "weekly-backup-d2" in text


def test_format_backup_budget_flags_a_b2_volume_left_on_the_daily_tier():
    """A PVC provisioned from the longhorn StorageClass lands in `default` until a deploy
    reconciles its label, which on B2 means a prune every night against a weekly budget."""
    vols = {"pvc-new": {"prune": 300, "blocks": 200, "backups": 4}}
    text, code = longhorn.format_backup_budget(vols, {"pvc-new": "default"})
    assert code == 1
    assert "ON THE DAILY TIER AND ON B2" in text and "pvc-new" in text


def test_stranded_counts_backups_the_current_tier_does_not_own():
    """Stranded means "no job will ever prune this", not "past retain".

    Longhorn's retain counts only a job's OWN backups, so a daily-era backup on a volume that
    has since moved to a weekday shard is pruned by nothing, ever. Until 2026-08-19 this was
    computed as `max(0, backups - retain)`, which under-reported the live cluster by 4.7x — 7
    against a true 33 — on the number an operator reads before deciding what to delete.
    """
    vols = {"pvc-moved": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-moved": {"daily-backup": 4, "weekly-backup-d2": 1}}
    text, _ = longhorn.format_backup_budget(
        vols, {"pvc-moved": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "4 stranded backup(s)" in text, text


def test_backups_the_current_tier_owns_are_not_stranded_even_past_retain():
    """The owning job prunes them on its next run, so they are queued, not abandoned."""
    vols = {"pvc-busy": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-busy": {"weekly-backup-d2": 5}}
    text, _ = longhorn.format_backup_budget(
        vols, {"pvc-busy": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "stranded" not in text, text


def test_stranded_falls_back_to_zero_without_ownership_data():
    """No owners map means nothing is PROVEN stranded — never guess high and prompt a delete."""
    vols = {"pvc-x": {"prune": 10, "blocks": 100, "backups": 5}}
    text, _ = longhorn.format_backup_budget(
        vols, {"pvc-x": "weekly-backup-d2"}, retain=2
    )
    assert "stranded" not in text, text


def test_format_backup_budget_reports_stranded_backups_not_pending_deletes():
    """Stranded backups are abandoned, not queued.

    Longhorn enforces retain only when the owning job runs against a volume still in its groups,
    counting only its own backups — so a volume that moved tier keeps its old backups forever and
    only the reaper clears them.

    This asserted `backups - retain` until 2026-08-19, which is a different quantity and made the
    check ratify the bug: 11 backups against retain 4 read as 7 stranded, when the answer depends
    entirely on who owns them. Here the d5 job owns 2, so the other 9 are the strays.
    """
    vols = {"pvc-a": {"prune": 100, "blocks": 50, "backups": 11}}
    owners = {"pvc-a": {"daily-backup": 9, "weekly-backup-d5": 2}}
    text, code = longhorn.format_backup_budget(
        vols, {"pvc-a": "weekly-backup-d5"}, retain=4, owners=owners
    )
    assert code == 0
    assert "9 stranded backup(s)" in text and "reaper" in text


def test_format_backup_budget_does_not_charge_a_day_for_an_unscheduled_volume():
    """A volume with no recurring job never runs a backup and so never prunes — charging its
    blocks to a shard would read as an over-budget day that cannot actually happen."""
    vols = {"pvc-idle": {"prune": 99999, "blocks": 9000, "backups": 3}}
    text, code = longhorn.format_backup_budget(vols, {"pvc-idle": "no-backup"})
    assert code == 0
    assert "never pruned" in text and "pvc-idle" in text
