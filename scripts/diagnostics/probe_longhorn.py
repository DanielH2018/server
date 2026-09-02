"""Longhorn's B2 backup objects: what the estate holds, what it costs, and what block size it's on.

Backs the `b2-longhorn`, `b2-budget` and `longhorn-blocks` subcommands. B2 publishes no usage
API, so most of this is reconstruction — from a listing of the backup store and from live
Volume CRs. See probe_b2_ledger for the transaction ledger these commands record into.
"""

import json
import subprocess
from urllib.parse import urlencode

import probe_b2_ledger as ledger
import probe_core as core
from probe_core import DEFAULT_TIMEOUT

LONGHORN_PREFIX = "longhorn"
B2_API_VERSION = "v3"
B2_AUTHORIZE_URL = (
    f"https://api.backblazeb2.com/b2api/{B2_API_VERSION}/b2_authorize_account"
)


# This used to run `rclone lsf` inside the kopia container. Both halves of that are gone:
# the k3s migration removed Docker from daniel-box and daniel-server (2026-08-14), and kopia
# itself was retired — its `kopia_b2_*` secrets are Longhorn's credentials now. The command
# therefore died with `FileNotFoundError: 'docker'` on every real run, while the tests kept
# passing because they only ever exercised the argv builder and the parser. B2's native API
# needs no SigV4 signing, so plain curl replaces both dependencies.
def b2_curl(config_body, timeout=DEFAULT_TIMEOUT):
    """One B2 API call, with url and credentials fed through curl's stdin config.

    Same guard as the HA and *arr helpers above: neither the application key nor the
    session token may appear in argv, where `ps` would expose them to any local user.
    """
    out = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "--config", "-"],
        input=config_body,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit("B2 request failed: " + out.stderr.strip()[:400])
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        # B2 reports a refused transaction cap as a JSON error, so a non-JSON body here
        # is a different problem (proxy, DNS, truncation) and is worth showing verbatim.
        # `from None`: the decode error's offset says nothing the body above doesn't.
        raise SystemExit(
            "B2 returned a non-JSON body: " + out.stdout.strip()[:200]
        ) from None


def b2_authorize_config(key_id, app_key):
    return f'url = "{B2_AUTHORIZE_URL}"\nuser = "{key_id}:{app_key}"\n'


def b2_list_files_config(api_url, token, bucket_id, prefix, start=None):
    """Build the `curl --config -` body (via stdin) for one B2 `b2_list_file_names` page.

    Args:
        api_url: The B2 API base URL from the authorize response.
        token: The B2 auth token.
        bucket_id: The bucket to list.
        prefix: The file-name prefix to list under.
        start: The `startFileName` cursor to resume a truncated listing, if any.
    """
    query = {
        "bucketId": bucket_id,
        "prefix": prefix.rstrip("/") + "/",
        "maxFileCount": "1000",
    }
    if start:
        query["startFileName"] = start
    url = f"{api_url}/b2api/{B2_API_VERSION}/b2_list_file_names?{urlencode(query)}"
    return f'url = "{url}"\nheader = "Authorization: {token}"\n'


def b2_list_buckets_config(api_url, token, account_id, bucket_name):
    query = {"accountId": account_id, "bucketName": bucket_name}
    url = f"{api_url}/b2api/{B2_API_VERSION}/b2_list_buckets?{urlencode(query)}"
    return f'url = "{url}"\nheader = "Authorization: {token}"\n'


def b2_longhorn_lines(
    key_id, app_key, bucket, prefix=LONGHORN_PREFIX, _call=b2_curl, _stats=None
):
    """List the Longhorn prefix, returning `path;size` lines.

    The shape is rclone's `lsf --format ps --separator ;` verbatim, and the paths are made
    relative to the prefix the same way rclone's did — so parse_longhorn_listing below is
    unchanged and its tests still describe the real input. Leaving the paths absolute would
    match none of its patterns and report a healthy bucket as "no Longhorn backup objects".
    """
    auth = _call(b2_authorize_config(key_id, app_key))
    storage = auth.get("apiInfo", {}).get("storageApi", {})
    api_url, token = storage.get("apiUrl"), auth.get("authorizationToken")
    if not api_url or not token:
        raise SystemExit("B2 authorize returned no apiUrl/authorizationToken")

    # A bucket-scoped application key already names its bucket; an account-wide one does not
    # and has to be looked up.
    bucket_id = storage.get("bucketId")
    if not bucket_id:
        listed = _call(
            b2_list_buckets_config(api_url, token, auth.get("accountId", ""), bucket)
        )
        buckets = listed.get("buckets", [])
        if not buckets:
            raise SystemExit(f"B2 has no bucket named {bucket}")
        bucket_id = buckets[0]["bucketId"]

    strip = prefix.rstrip("/") + "/"
    lines, start = [], None
    pages = 0
    while True:
        page = _call(b2_list_files_config(api_url, token, bucket_id, prefix, start))
        pages += 1
        for entry in page.get("files", []):
            name = entry.get("fileName", "")
            if name.startswith(strip):
                name = name[len(strip) :]
            lines.append(f"{name};{entry.get('contentLength', 0)}")
        start = page.get("nextFileName")
        if not start:
            # Each page is one b2_list_file_names, and the authorize that preceded them is
            # itself billable — both Class C. Reported through an out-param so the existing
            # callers and their tests keep the plain list return.
            if _stats is not None:
                _stats["class_c"] = pages + 1
                _stats["pages"] = pages
            return lines


def parse_longhorn_listing(lines):
    """Aggregate `rclone lsf --format ps` output per Longhorn volume.

    Longhorn lays a backup out as
    `backupstore/volumes/<aa>/<bb>/<volume>/{volume.cfg,backups/*.cfg,blocks/**/*.blk}`.
    The `.blk` files are the actual DATA; the `.cfg` files are only metadata, and that
    distinction is the entire point of this tool — a backup can be registered and report
    `Completed` in Longhorn while what actually landed in B2 is metadata describing blocks
    that are not there. Counting blocks is what makes "the data really is in B2" checkable.
    """
    vols = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        path, _, size = line.rpartition(";")
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:  # volumes/<aa>/<bb>/<volume>/...
            continue
        v = vols.setdefault(parts[i + 3], {"blocks": 0, "block_bytes": 0, "cfgs": 0})
        try:
            nbytes = int(size)
        except ValueError:
            nbytes = 0
        if path.endswith(".blk"):
            v["blocks"] += 1
            v["block_bytes"] += nbytes
        elif path.endswith(".cfg"):
            v["cfgs"] += 1
    return vols


def format_longhorn_summary(vols):
    """Render the per-volume table; non-zero exit if any volume has metadata but no data."""
    if not vols:
        return "no Longhorn backup objects found under the prefix", 1
    width = max(len(n) for n in vols)
    rows, bad = [], []
    for name in sorted(vols):
        v = vols[name]
        rows.append(
            "%-*s  %6d blocks  %8.1f MB  %3d cfg"
            % (width, name, v["blocks"], v["block_bytes"] / 1e6, v["cfgs"])
        )
        if v["blocks"] == 0:
            bad.append(name)
    out = "\n".join(rows)
    if bad:
        out += "\n\nNO DATA BLOCKS for: %s — metadata only, not restorable" % ", ".join(
            bad
        )
    return out, (1 if bad else 0)


# B2's free tier allows 2,500 Class C transactions a day. Longhorn's retention delete is the
# thing that spends them: DeleteDeltaBlockBackup walks the volume's whole block tree with one
# delimited ListObjects per directory (backupstore deltablock.go getBlockNamesForVolume), and it
# runs once per deleted backup. So a shard's daily Class C cost is set by how many BLOCKS its
# volumes hold, not by how much data changed — which is why this is worth watching as volumes
# grow, and why the seventh cap event was not preventable by looking at bytes.
B2_CLASS_C_DAILY_CAP = 2500
# Headroom for everything this model does not count: monitor-bridge's two B2 probes
# (check_b2_reachable, and check_b2_storage's daily listing), and the per-prune extras beyond the
# block walk — the deletion lock check, the backups/ name list, and one cfg GET per retained
# backup. NOT kopia: it was retired with the k3s migration and issues no B2 traffic at all. Only
# the `kopia_b2_*` SOPS key names survive, and they are Longhorn's credentials now.
B2_BUDGET_RESERVE = 400


def parse_backup_budget(lines):
    """Per-volume backup count, block count, and the Class C cost of one retention prune.

    Takes the same `path;size` lines as parse_longhorn_listing. A prune costs one
    ListObjects for `blocks/`, one per first-level hash directory, and one per second-level
    directory — so the DIRECTORY counts, not the block count, are the price.
    """
    vols = {}
    for line in lines:
        path = line.strip().rpartition(";")[0]
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:
            continue
        v = vols.setdefault(
            parts[i + 3], {"backups": 0, "blocks": 0, "lv1": set(), "lv2": set()}
        )
        tail = parts[i + 4 :]
        if tail[:1] == ["backups"] and path.endswith(".cfg"):
            v["backups"] += 1
        elif path.endswith(".blk") and tail[:1] == ["blocks"] and len(tail) >= 4:
            v["blocks"] += 1
            v["lv1"].add(tail[1])
            v["lv2"].add((tail[1], tail[2]))
    for v in vols.values():
        v["prune"] = 1 + len(v["lv1"]) + len(v["lv2"])
    return vols


def format_backup_budget(vols, shards, names=None, retain=2, owners=None):
    """Render the per-shard Class C projection; non-zero exit if a shard is over budget.

    `shards` maps volume name to its recurring-job group. A volume with no group never runs a
    backup and never prunes, so it is reported separately rather than charged to a day.
    """
    names = names or {}
    budget = B2_CLASS_C_DAILY_CAP - B2_BUDGET_RESERVE
    byshard, idle, daily = {}, [], []
    for vol, v in vols.items():
        shard = shards.get(vol)
        if shard and shard.startswith("weekly-backup-"):
            byshard.setdefault(shard, []).append((vol, v))
        elif shard in (None, "no-backup"):
            idle.append(vol)
        else:
            # A PVC provisioned from the `longhorn` StorageClass lands in `default` — the DAILY
            # group — until the next deploy reconciles its label. On B2 that is a prune every
            # night against a budget sized for one a week, so it is the loudest thing here.
            daily.append(vol)

    rows, over = [], []
    for shard in sorted(byshard):
        members = sorted(byshard[shard], key=lambda kv: -kv[1]["prune"])
        total = sum(v["prune"] for _, v in members)
        # Backups beyond `retain` are STRANDED, not queued for deletion. Longhorn enforces
        # retain only when the owning RecurringJob runs against a volume still in its `groups:`,
        # and it counts only ITS OWN backups — so the daily-era backups on a volume that moved to
        # a weekday shard are pruned by nothing, ever. Measured 2026-08-17: radarr-config sat at
        # 4 daily-backup + 1 weekly-backup-d2 against retain 4 and deleted none, because the
        # weekly job saw 1 of its own. `longhorn-reap-orphan-backups.sh` is what clears these.
        #
        # The consequence for this projection: a shard's prune cost does not begin until that
        # job has more than `retain` of its own backups, and until then its blocks only grow.
        # STRANDED means "no job will ever prune this", which is NOT the same as "past retain"
        # — and until 2026-08-19 this line computed the latter, `max(0, backups - retain)`,
        # while the comment above described the former. It under-reported by 4.7x: 7 against a
        # true 33, on the number an operator reads before deciding what to delete.
        #
        # A backup is stranded when the job that produced it is not the job that now selects the
        # volume. Longhorn's retain counts only a job's OWN backups, so a daily-era backup on a
        # volume since moved to a weekday shard is pruned by nothing, ever — regardless of how
        # many backups the volume has in total. Anything the current tier owns is NOT stranded
        # even when it sits past retain, because that job prunes it on its next run.
        # No ownership data means nothing is PROVEN stranded, so claim nothing. Falling back to
        # `backups - retain` is what produced the wrong number, and this figure is read
        # immediately before someone deletes a backup.
        stranded = (
            0
            if owners is None
            else sum(
                max(0, v["backups"] - owners.get(vol, {}).get(shard, 0))
                for vol, v in members
            )
        )
        flag = "OVER" if total > budget else "ok"
        if total > budget:
            over.append(shard)
        rows.append(
            "%-17s %5d C  %s%s"
            % (
                shard,
                total,
                flag,
                "  (%d stranded backup(s) — see the orphan-backup reaper)" % stranded
                if stranded
                else "",
            )
        )
        for vol, v in members:
            rows.append(
                "    %-22s %5d C  %5d blocks  %2d backups"
                % (names.get(vol, vol)[:22], v["prune"], v["blocks"], v["backups"])
            )
    if idle:
        rows.append(
            "no-backup (never pruned): %s"
            % ", ".join(sorted(names.get(v, v) for v in idle))
        )
    if daily:
        over.append("daily-on-B2")
        rows.append(
            "ON THE DAILY TIER AND ON B2: %s — %d C every night, not once a week. "
            "Route to r2 or move to a weekly shard."
            % (
                ", ".join(sorted(names.get(v, v) for v in daily)),
                sum(vols[v]["prune"] for v in daily),
            )
        )
    rows.append(
        "budget per day: %d Class C (cap %d less %d reserved for the B2 probes and prune overhead)"
        % (budget, B2_CLASS_C_DAILY_CAP, B2_BUDGET_RESERVE)
    )
    if over:
        rows.append("OVER BUDGET: %s — rebalance before the next run" % ", ".join(over))
    return "\n".join(rows), (1 if over else 0)


def volume_shard_labels(_run=None):
    """{longhorn volume: recurring-job group} from the live cluster."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "volumes.longhorn.io",
            "-o",
            "json",
        ]
    )
    if out.returncode != 0:
        raise SystemExit("kubectl failed: " + out.stderr.strip()[:300])
    shards = {}
    for item in json.loads(out.stdout).get("items", []):
        for key in item.get("metadata", {}).get("labels", {}):
            if key.startswith("recurring-job-group.longhorn.io/"):
                shards[item["metadata"]["name"]] = key.split("/", 1)[1]
    return shards


def volume_owned_backup_counts(_run=None):
    """{longhorn volume: how many of its backups its CURRENT recurring job owns}.

    Longhorn stamps the producing job onto `.status.labels.RecurringJob`. That is a Longhorn
    STATUS field, not a Kubernetes label, so `kubectl -l` cannot select on it.
    """
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        ["kubectl", "-n", "longhorn-system", "get", "backups.longhorn.io", "-o", "json"]
    )
    if out.returncode != 0:
        raise SystemExit("kubectl failed: " + out.stderr.strip()[:300])
    owners = {}
    for item in json.loads(out.stdout).get("items", []):
        status = item.get("status", {})
        vol = status.get("volumeName")
        job = (status.get("labels") or {}).get("RecurringJob")
        if vol and job:
            owners.setdefault(vol, {})[job] = owners.setdefault(vol, {}).get(job, 0) + 1
    return owners


def pvc_names(_run=None):
    """{longhorn volume: PVC name}, so the report reads in service terms."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(["kubectl", "get", "pv", "-o", "json"])
    if out.returncode != 0:
        return {}
    return {
        pv["metadata"]["name"]: pv["spec"].get("claimRef", {}).get("name", "")
        for pv in json.loads(out.stdout).get("items", [])
        if pv.get("spec", {}).get("claimRef")
    }


def run_b2_budget(ns):
    """Project each weekly shard's Class C spend against B2's free-tier daily cap.

    One listing (~10 Class C), so it is cheap enough to run weekly and far too expensive to
    put in the 10-minute monitor cron.
    """
    if ns.dry_run:
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names prefix={ns.prefix.rstrip('/')}/ "
            "plus kubectl get volumes.longhorn.io,pv"
        )
        return 0

    stats = {}
    lines = b2_longhorn_lines(
        core.sops_extract("kopia_b2_key_id"),
        core.sops_extract("kopia_b2_application_key"),
        ns.bucket or core.sops_extract("kopia_b2_bucket"),
        ns.prefix,
        _stats=stats,
    )
    ledger.record_b2_spend(
        "b2-budget",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    text, code = format_backup_budget(
        parse_backup_budget(lines),
        volume_shard_labels(),
        pvc_names(),
        ns.retain,
        volume_owned_backup_counts(),
    )
    print(text)
    return code


def run_b2_longhorn(ns):
    """Prove Longhorn's backups hold real data blocks in B2, not just metadata.

    The design doc's §6 gate is that a service's data must be visible in its NEW backup
    path before the Docker copy is decommissioned, so slice 2 needs this per service.

    Costs a handful of transactions (one listing, paged at 1000 objects) — negligible
    against the daily free allowance, but not free: don't put it in a loop.
    """
    # Ahead of the credential read: --dry-run exists to describe the call WITHOUT making it,
    # so it must not decrypt anything either.
    if ns.dry_run:
        bucket = ns.bucket or "<kopia_b2_bucket>"
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names "
            f"bucket={bucket} prefix={ns.prefix.rstrip('/')}/"
        )
        return 0

    bucket = ns.bucket or core.sops_extract("kopia_b2_bucket")
    stats = {}
    lines = b2_longhorn_lines(
        core.sops_extract("kopia_b2_key_id"),
        core.sops_extract("kopia_b2_application_key"),
        bucket,
        ns.prefix,
        _stats=stats,
    )
    ledger.record_b2_spend(
        "b2-longhorn",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    text, code = format_longhorn_summary(parse_longhorn_listing(lines))
    print(text)
    return code


# --- longhorn-blocks: is every B2-tier volume on 16 MiB blocks? --------------------------------

# 16 MiB. The migration's whole point: it cuts B2 prune, backup and restore cost ~8x, and
# `default-backup-block-size` is IMMUTABLE per volume, so only volumes created after the change
# get it. That is why this is a census of live state rather than a setting to read once.
LONGHORN_WEEKLY_BLOCK_BYTES = 16 * 1024 * 1024

_RECURRING_GROUP_PREFIX = "recurring-job-group.longhorn.io/"


def volume_tier_census(volumes):
    """Group Longhorn Volume CRs by RecurringJob group, target and backup block size.

    The GROUP decides the tier, not `spec.backupTargetName`. `default` is literally the default
    target name, so every volume that was never moved to another target reports it — including
    the ones no job selects at all. Grouping by target alone therefore reads 18 unbacked
    volumes as members of the B2 tier, which is the same trap
    `seed-backups-do-not-count-as-rotation-coverage` records on the coverage side.
    """
    rows = {}
    for item in (volumes or {}).get("items", []):
        spec = item.get("spec") or {}
        labels = (item.get("metadata") or {}).get("labels") or {}
        groups = sorted(
            key[len(_RECURRING_GROUP_PREFIX) :]
            for key in labels
            if key.startswith(_RECURRING_GROUP_PREFIX)
        )
        key = (
            ",".join(groups) or "-",
            spec.get("backupTargetName") or "-",
            str(spec.get("backupBlockSize") or "-"),
        )
        rows.setdefault(key, []).append((item.get("metadata") or {}).get("name", "?"))
    return rows


def weekly_volumes_off_block_size(rows, expected=LONGHORN_WEEKLY_BLOCK_BYTES):
    """Names of weekly-shard volumes NOT on the expected block size.

    Only `weekly-backup-*` is asserted. `no-backup` volumes are unconstrained — nothing backs
    them up, so their block size cannot cost anything — and the R2/daily volumes are a recorded
    exception, immutable in place and not worth recreating.
    """
    offenders = []
    for (group, _target, block), names in rows.items():
        if not group.startswith("weekly-backup-"):
            continue
        if block != str(expected):
            offenders.extend(f"{n} ({group}, blockSize={block})" for n in names)
    return sorted(offenders)


def format_block_census(rows, expected=LONGHORN_WEEKLY_BLOCK_BYTES):
    """Render the census, and fail when a weekly-shard volume is off the expected size."""
    lines = []
    for (group, target, block), names in sorted(rows.items()):
        mib = int(block) // (1024 * 1024) if block.isdigit() else "?"
        lines.append(
            f"  group={group:<20} target={target:<8} block={mib}MiB  count={len(names)}"
        )
    offenders = weekly_volumes_off_block_size(rows, expected)
    if offenders:
        lines.append("")
        lines.append(
            f"FAIL: {len(offenders)} weekly-shard volume(s) are not on "
            f"{expected // (1024 * 1024)} MiB blocks. Block size is immutable per volume, so "
            "the fix is migrate_volume_block_size.yml followed by a seed backup:"
        )
        lines.extend(f"    {o}" for o in offenders)
        return "\n".join(lines), 1
    lines.append("")
    lines.append(
        f"OK: every weekly-shard volume is on {expected // (1024 * 1024)} MiB blocks."
    )
    return "\n".join(lines), 0


def run_longhorn_blocks(ns):
    """Census live Volume CRs by tier and backup block size (read-only, spends no B2)."""
    argv = [
        "kubectl",
        "-n",
        "longhorn-system",
        "get",
        "volumes.longhorn.io",
        "-o",
        "json",
    ]
    if getattr(ns, "dry_run", False):
        print(" ".join(argv))
        return 0
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"cannot list Longhorn volumes: {proc.stderr.strip()}")
        return 2
    text, code = format_block_census(volume_tier_census(json.loads(proc.stdout)))
    print(text)
    return code
