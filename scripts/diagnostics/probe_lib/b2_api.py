"""B2's native API: the authorized calls, the Longhorn listing they page, and its parser.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Everything here is about
Backblaze rather than about Longhorn's cluster state: building a `curl --config -` body,
paging `b2_list_file_names` under a prefix, and turning that listing into per-volume block
and metadata counts.

longhorn.py keeps the `b2-longhorn` and `b2-budget` subcommands that drive this.
`longhorn_budget.py` prices a retention prune from the same listing, `longhorn_cluster.py`
reads the live Volume/Backup/PV objects, and `longhorn_blocks.py` censuses block sizes.
"""

import json
import subprocess
from urllib.parse import urlencode

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.core import DEFAULT_TIMEOUT

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
