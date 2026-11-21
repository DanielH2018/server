"""The B2 Object Lock row ADR-0041 adds."""

import email.message
import urllib.error
from dataclasses import replace

import pytest

import checks.b2
from _fake_sources import FakeSources
from bridge.types import CheckResult
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from verdicts.storage import B2_PRUNE_AGE_DAYS, b2_lock_verdict

SEVEN_DAYS = {"mode": "governance", "period": {"duration": 7, "unit": "days"}}


def _bucket(enabled: bool = True, retention: dict | None = SEVEN_DAYS) -> dict:
    return {
        "bucketName": "weekly",
        "fileLockConfiguration": {
            "isClientAuthorizedToRead": True,
            "value": {"isFileLockEnabled": enabled, "defaultRetention": retention},
        },
    }


GOVERNED = _bucket()
WRITER = [
    "listBuckets",
    "writeFiles",
    "deleteFiles",
    "readBucketRetentions",
    "writeFileRetentions",
]
AUTH = {
    "accountId": "acct",
    "authorizationToken": "tok",
    "apiInfo": {
        "storageApi": {
            "apiUrl": "https://api",
            "bucketId": "b1",
            "capabilities": WRITER,
        }
    },
}


def test_a_governed_bucket_and_a_key_without_bypass_is_clean():
    ok, msg = b2_lock_verdict(GOVERNED, WRITER)
    assert ok
    assert "governance, 7 days" in msg


@pytest.mark.parametrize(
    "bucket, capabilities, detail",
    [
        (_bucket(enabled=False), WRITER, "lock off"),
        (
            {**GOVERNED, "fileLockConfiguration": {"isClientAuthorizedToRead": False}},
            WRITER,
            "lock unreadable",
        ),
        (
            _bucket(retention={"mode": None, "period": None}),
            WRITER,
            "no default retention",
        ),
        (GOVERNED, [*WRITER, "bypassGovernance"], "key bypasses governance"),
        (
            GOVERNED,
            [c for c in WRITER if c != "writeFileRetentions"],
            "key cannot extend retention",
        ),
        ({"bucketName": "weekly"}, WRITER, "no lock config"),
        (
            _bucket(retention={**SEVEN_DAYS, "mode": "compliance"}),
            WRITER,
            "compliance mode",
        ),
        (
            _bucket(
                retention={**SEVEN_DAYS, "period": {"duration": 14, "unit": "days"}}
            ),
            WRITER,
            "retention outlasts the prune",
        ),
        (
            _bucket(
                retention={**SEVEN_DAYS, "period": {"duration": 1, "unit": "years"}}
            ),
            WRITER,
            "retention outlasts the prune",
        ),
    ],
    ids=[
        "lock off",
        "unreadable",
        "no default retention",
        "key bypasses governance",
        "key cannot extend retention",
        "no lock config",
        "compliance",
        "as long as the prune",
        "a year",
    ],
)
def test_a_bucket_that_does_not_bind_the_writer_is_flagged(
    bucket, capabilities, detail
):
    result = CheckResult.of(b2_lock_verdict(bucket, capabilities))
    assert not result.ok
    assert result.details == (detail,)


@pytest.fixture
def b2_cfg(cfg):
    return replace(cfg, B2_PROBE_KEY_ID="kid", B2_PROBE_APPLICATION_KEY="key")


def test_the_b2_row_lists_the_keys_bucket_and_holds_the_verdict_a_day(b2_cfg):
    src = FakeSources(
        get_json=lambda url, headers: AUTH,
        post_json=lambda url, payload, headers: {"buckets": [GOVERNED]},
    )
    ok, _ = checks.b2.check_b2_object_lock(b2_cfg, src, now=1000.0)
    assert ok
    assert src.calls[1][1][:2] == (
        "https://api/b2api/v3/b2_list_buckets",
        {"accountId": "acct", "bucketId": "b1"},
    )
    later = FakeSources(state=src.state)
    ok, msg = checks.b2.check_b2_object_lock(b2_cfg, later, now=1000.0 + 86399)
    assert ok
    assert "checked" in msg
    assert later.calls == []


def test_a_b2_refusal_is_cached_because_b2_billed_it(b2_cfg):
    def refuse(url, payload, headers):
        err = urllib.error.HTTPError(
            url, 401, "unauthorized", email.message.Message(), None
        )
        err.close()
        raise err

    src = FakeSources(get_json=lambda url, headers: AUTH, post_json=refuse)
    ok, msg = checks.b2.check_b2_object_lock(b2_cfg, src, now=1000.0)
    assert not ok
    assert "refused" in msg
    later = FakeSources(state=src.state)
    assert not checks.b2.check_b2_object_lock(b2_cfg, later, now=1060.0)[0]
    assert later.calls == []


def test_the_b2_bound_is_the_age_the_weekly_prune_deletes_at():
    defaults = yaml_fast.safe_load(
        (K8S_ROLES / "longhorn-backup" / "defaults" / "main.yml").read_text()
    )
    assert B2_PRUNE_AGE_DAYS == 7 * defaults["k3s_longhorn_weekly_backup_retain"]
