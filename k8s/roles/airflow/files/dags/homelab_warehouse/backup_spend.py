"""Roll one day of Longhorn backups up into `longhorn.backup_daily`, and check it.

Longhorn logs one line per backup, naming the delta it walked:

    [pvc-…-r-…] … msg="Created snapshot changed blocks: 46 mappings, 46 blocks and 45 new blocks"

It issues one HeadObject per walked block, so `blocks` is the backup's Class B transaction
count, and `new_blocks` is what it uploaded. The lines live in loki-homelab, which keeps 31
days. Each volume's backup target and claim come from its Longhorn Volume object, read through
the Kubernetes API, since the line names neither. The join reads each volume's target at load
time, so a volume moved between tiers is attributed wholly to its new target.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    lines = fetch_backup_lines(LOKI_URL, day)
    rows = backup_rows(day, lines, volumes_by_name(fetch_volumes()))
"""

import json
import re
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from .prom_rollup import get_json

LOKI_URL = "http://loki-homelab.homelab.svc.cluster.local:3100"
# loki_homelab_retention (744h) in the loki-homelab role.
LOKI_KEEP_DAYS = 31
PAGE_LIMIT = 5000
BACKUP_LINES = '{namespace="longhorn-system"} |= "Created snapshot changed blocks"'
BLOCKS = re.compile(
    r"Created snapshot changed blocks: \d+ mappings, (\d+) blocks and (\d+) new blocks"
)
REPLICA = re.compile(r"\[(pvc-[0-9a-f-]{36})-r-[0-9a-f]+\]")
UNATTRIBUTED = "unattributed"

API_URL = "https://kubernetes.default.svc"
VOLUMES_PATH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"
TOKEN_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

SCHEMA = "longhorn"
BACKUP_TABLE = f"{SCHEMA}.backup_daily"
BACKUP_COLUMNS = (
    "day",
    "volume",
    "claim",
    "target",
    "backups",
    "class_b",
    "new_blocks",
)
DDL = (
    f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (day date NOT NULL, volume text NOT NULL, "
    "claim text NOT NULL, target text NOT NULL, backups integer NOT NULL, "
    "class_b bigint NOT NULL, new_blocks bigint NOT NULL, PRIMARY KEY (day, volume))",
)


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def in_backfill_window(day: date, now: datetime) -> bool:
    """Whether `day` is complete and a day inside loki-homelab's retention, so wholly held."""
    start = _start(day)
    return (
        now - timedelta(days=LOKI_KEEP_DAYS - 1) < start
        and start + timedelta(days=1) <= now
    )


def fetch_backup_lines(
    base_url: str, day: date, opener: Callable | None = None
) -> list[str]:
    """Every backup line Longhorn logged over `day`.

    Raises:
      RuntimeError: The day held `PAGE_LIMIT` lines or more, so the page may be cut short.
    """
    params = urllib.parse.urlencode(
        {
            "query": BACKUP_LINES,
            "start": int(_start(day).timestamp() * 1e9),
            "end": int((_start(day) + timedelta(days=1)).timestamp() * 1e9),
            "limit": PAGE_LIMIT,
            "direction": "forward",
        }
    )
    kwargs = {"opener": opener} if opener else {}
    streams = get_json(f"{base_url}/loki/api/v1/query_range?{params}", **kwargs)
    lines = [line for stream in streams["result"] for _, line in stream["values"]]
    if len(lines) >= PAGE_LIMIT:
        raise RuntimeError(f"{day}: Loki returned {len(lines)} backup lines, the limit")
    return lines


def fetch_volumes(
    api_url: str = API_URL,
    token_dir: str = TOKEN_DIR,
    opener: Callable = urllib.request.urlopen,
    ssl_context: Callable = ssl.create_default_context,
) -> list[dict]:
    """Every Longhorn Volume object, read with the pod's ServiceAccount token."""
    with open(f"{token_dir}/token") as f:
        token = f.read().strip()
    request = urllib.request.Request(
        f"{api_url}{VOLUMES_PATH}", headers={"Authorization": f"Bearer {token}"}
    )
    context = ssl_context(cafile=f"{token_dir}/ca.crt")
    with opener(request, timeout=60, context=context) as response:
        return json.load(response)["items"]


def volumes_by_name(items: Sequence[Mapping]) -> dict[str, tuple[str, str]]:
    """Each volume's `(claim, target)`, where claim is `namespace/name` or empty."""
    volumes = {}
    for item in items:
        k8s = item.get("status", {}).get("kubernetesStatus", {})
        claim = f"{k8s['namespace']}/{k8s['pvcName']}" if k8s.get("pvcName") else ""
        volumes[item["metadata"]["name"]] = (
            claim,
            item.get("spec", {}).get("backupTargetName", ""),
        )
    return volumes


def backup_rows(
    day: date, lines: Sequence[str], volumes: Mapping[str, tuple[str, str]]
) -> list[tuple]:
    """One `backup_daily` row per volume that backed up in the day.

    A line whose replica prefix the log pipeline trimmed still counts, under `UNATTRIBUTED`,
    because dropping it would understate spend. A volume with no Volume object, such as one
    deleted since, gets an empty claim and target.
    """
    sums: dict[str, list[int]] = {}
    for line in lines:
        blocks = BLOCKS.search(line)
        if not blocks:
            continue
        replica = REPLICA.search(line)
        volume = replica.group(1) if replica else UNATTRIBUTED
        entry = sums.setdefault(volume, [0, 0, 0])
        entry[0] += 1
        entry[1] += int(blocks.group(1))
        entry[2] += int(blocks.group(2))
    return [
        (day, volume, *volumes.get(volume, ("", "")), *counts)
        for volume, counts in sorted(sums.items())
    ]


def quality_problems(day: date, rows: Sequence[Mapping]) -> list[str]:
    """Every reason the day's `backup_daily` rows are not trustworthy; empty when they are.

    A backup shard runs every day, so a day with no backup line means Longhorn's lines were
    lost or no backup ran, and either is worth a failed run.
    """
    problems = []
    if not rows:
        problems.append(f"{day}: no backup line")
    for row in rows:
        if row["new_blocks"] > row["class_b"]:
            problems.append(
                f"{day}: {row['volume']} uploaded {row['new_blocks']} blocks of "
                f"{row['class_b']} walked"
            )
    return problems
