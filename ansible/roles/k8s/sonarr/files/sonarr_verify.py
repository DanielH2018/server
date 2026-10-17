"""sonarr's verify Job: prove the library resolves inside sonarr's pod, read through its API.

Inert until sonarr's flip: `templates/verify-inert/` holds the Job, and the flip PR moves it to
`templates/verify/`, where the renderer emits it into `sonarr-verify` (target slice D5, *A
verify conversion lands as inert code*). It replaces `tasks/verify.yml`'s two API reads and
their asserts, "Fail if a root folder does not resolve inside the pod" and "Fail if the
cluster library is not intact", with the same predicates. Sonarr answers both from its own
pod, so unlike tdarr's Job this one checks the live pod and not a copy of its wiring.

Inputs come from the Job's environment: `SONARR_URL`, the Service's in-cluster address, and
`SONARR_API_KEY`. `SONARR_DEPLOYMENT_DIGEST8` is in the spec only to rename the Job, and the
script prints it. Nothing here prints the key, which travels as a header. The first connection
goes through `flux_job_connect.open_url`, which retries a refusal while kube-router learns the
new pod's address. Exit 0 when every check passes, 1 when one fails, naming it.

Run: python3 sonarr_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/sonarr/tests/test_sonarr_verify.py
"""

import argparse
import json
import os
import sys
import urllib.error
from collections.abc import Mapping

from flux_job_connect import Request, open_url

# The socket timeout of each API read, in seconds.
TIMEOUT = 30
GIB = 1073741824


class Failed(Exception):
    """A check failed; the message names what is wrong."""


def read(base_url: str, api_key: str, path: str) -> list:
    """GET `/api/v3/<path>` and return the JSON list sonarr answers with.

    Raises:
        Failed: sonarr answered with an error status, sent no answer, or answered with
            something other than a JSON list.
    """
    request = Request(
        f"{base_url}/api/v3/{path}",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    try:
        with open_url(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        raise Failed(f"GET /api/v3/{path} answered HTTP {err.code}") from err
    except OSError as err:
        raise Failed(f"GET /api/v3/{path} got no answer: {err}") from err
    try:
        data = json.loads(body)
    except ValueError as err:
        raise Failed(f"GET /api/v3/{path} answered with no JSON") from err
    if not isinstance(data, list):
        raise Failed(f"GET /api/v3/{path} answered with JSON that is not a list")
    return data


def check_rootfolders(folders: list) -> str:
    """Prove every root folder resolves inside the pod, at a path under /data.

    `accessible` is sonarr's own answer to "can I stat this path right now", and `freeSpace`
    is read from the filesystem the mount leads to. sonarr stores absolute in-container paths,
    so a folder outside /data means the pod does not present the media tree where the
    database expects it.
    """
    if not folders:
        raise Failed("sonarr reports no root folder")
    broken = [
        f.get("path")
        for f in folders
        if not f.get("accessible")
        or not str(f.get("path", "")).startswith("/data/")
        or "freeSpace" not in f
    ]
    if broken:
        raise Failed(f"root folders that do not resolve in the pod: {broken}")
    free = folders[0]["freeSpace"] / GIB
    paths = ", ".join(f["path"] for f in folders)
    return f"root folders resolve inside the pod: {paths} (free space {free:.0f} GiB)"


def check_series(series: list) -> str:
    """Prove every series sits under /data and some episode files are measured on disk.

    A non-zero `sizeOnDisk` is read through the mount, so the pod is reading real files and
    not an empty tree that happens to exist. An empty library fails the second half.
    """
    outside = [
        s.get("path") for s in series if not str(s.get("path", "")).startswith("/data/")
    ]
    if outside:
        raise Failed(
            f"{len(series)} series, but these paths sit outside /data: {outside}"
        )
    sizes = [(s.get("statistics") or {}).get("sizeOnDisk") or 0 for s in series]
    if not any(sizes):
        raise Failed(f"{len(series)} series, and none has an episode file on disk")
    return (
        f"{len(series)} series, every path under /data, and {sum(sizes) / GIB:.1f} GiB of "
        "episode files measured through the mount"
    )


def run(env: Mapping[str, str]) -> list[str]:
    """Run both checks, and return one report line each.

    Raises:
        Failed: The first check that failed.
    """
    base_url, api_key = env["SONARR_URL"].rstrip("/"), env["SONARR_API_KEY"]
    return [
        f"verifying sonarr's Deployment {env.get('SONARR_DEPLOYMENT_DIGEST8', 'unknown')}",
        check_rootfolders(read(base_url, api_key, "rootfolder")),
        check_series(read(base_url, api_key, "series")),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"sonarr-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"sonarr-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
