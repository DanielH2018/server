"""The canned GitHub and GHCR answers the deploy-pipeline tests hand to `FakeSources`.

`github()` answers the family's five GETs by URL, as GitHub and GHCR answered on 2026-10-15.
A module with a leading underscore, for the reason `_fake_sources.py` gives: test_check_cli.py
runs whole cycles against it too, and the name is unique repo-wide.
"""

import hashlib
import io
import json
import urllib.error
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import Message

from _fake_sources import FakeSources

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc).timestamp()
PROD = "c" * 40
NEWER = "d" * 40
OLDER = "b" * 40


def stamp(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def manifest_bytes(commit: str = PROD) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "annotations": {
                "org.opencontainers.image.revision": "master@sha1:" + commit
            },
        }
    ).encode()


PROD_DIGEST = "sha256:" + hashlib.sha256(manifest_bytes()).hexdigest()


def green_run(sha: str, pushed_ago: float, green_ago: float) -> dict:
    return {
        "head_sha": sha,
        "created_at": stamp(pushed_ago),
        "updated_at": stamp(green_ago),
    }


def github(
    hold: str | None = "none",
    hold_ago: float = 3600,
    digest: str | None = PROD_DIGEST,
    runs: list[dict] | None = None,
    compare: str = "behind",
    manifest: bytes | None = manifest_bytes(),
    fail: str = "",
    prom_vector: Callable[[str], list] | None = None,
) -> FakeSources:
    """A `FakeSources` answering the family's five GETs.

    Args:
      hold: DEPLOY_HOLD's value, or None for a variable that does not exist.
      hold_ago: Seconds since DEPLOY_HOLD was written.
      digest: PROD_DIGEST's value, or None for a variable that does not exist.
      runs: master's green CI runs, newest first. None is `prod`'s own run alone.
      compare: The compare call's `status`.
      manifest: `prod`'s manifest bytes, or None for a registry with no `prod` tag.
      fail: A URL fragment whose GET raises, as an unreachable source does.
      prom_vector: The Prometheus answer, for a test that runs whole cycles.
    """
    variables = []
    if hold is not None:
        variables.append(
            {"name": "DEPLOY_HOLD", "value": hold, "updated_at": stamp(hold_ago)}
        )
    if digest is not None:
        variables.append(
            {"name": "PROD_DIGEST", "value": digest, "updated_at": stamp(60)}
        )
    listed = [green_run(PROD, 7200, 7000)] if runs is None else runs

    def get_json(url, headers=None):
        if fail and fail in url:
            raise RuntimeError("GET %s: timed out" % url)
        if url.startswith("https://ghcr.io/token"):
            return {"token": "anonymous-pull"}
        assert headers is not None
        assert headers["Authorization"] == "Bearer read-only-token"
        if url.endswith("/actions/variables"):
            return {"total_count": len(variables), "variables": variables}
        if "/actions/workflows/ci.yml/runs?" in url:
            return {"total_count": len(listed), "workflow_runs": listed}
        if "/compare/" in url:
            return {"status": compare}
        raise AssertionError("unexpected GET %s" % url)

    def get_bytes(url, headers=None):
        if fail and fail in url:
            raise RuntimeError("GET %s: timed out" % url)
        assert url == "https://ghcr.io/v2/danielh2018/homelab-manifests/manifests/prod"
        if manifest is None:
            missing = urllib.error.HTTPError(
                url, 404, "MANIFEST_UNKNOWN", Message(), io.BytesIO()
            )
            # The live `bridge.net._get` reads and closes the error before re-raising it.
            missing.close()
            raise missing
        return manifest

    if prom_vector is None:
        return FakeSources(get_json=get_json, get_bytes=get_bytes)
    return FakeSources(get_json=get_json, get_bytes=get_bytes, prom_vector=prom_vector)
