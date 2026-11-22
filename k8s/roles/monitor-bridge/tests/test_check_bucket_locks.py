"""The B2 Object Lock row ADR-0041 adds."""

import email.message
import urllib.error
from dataclasses import replace

import pytest

import checks.b2
from _fake_sources import FakeSources
from bridge.types import CheckResult
from verdicts.storage import b2_lock_verdict

NO_DEFAULT = {"mode": None, "period": None}
SEVEN_DAYS = {"mode": "governance", "period": {"duration": 7, "unit": "days"}}


def _bucket(enabled: bool = True, retention: dict | None = NO_DEFAULT) -> dict:
    return {
        "bucketName": "weekly",
        "fileLockConfiguration": {
            "isClientAuthorizedToRead": True,
            "value": {"isFileLockEnabled": enabled, "defaultRetention": retention},
        },
    }


FILE_LOCKED = _bucket()
WRITER = ["listBuckets", "writeFiles", "deleteFiles", "readBucketRetentions"]
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


def test_file_lock_with_no_default_retention_and_a_key_without_bypass_is_clean():
    ok, msg = b2_lock_verdict(FILE_LOCKED, WRITER)
    assert ok
    assert "no default retention" in msg


@pytest.mark.parametrize(
    "bucket, capabilities, detail",
    [
        (_bucket(enabled=False), WRITER, "lock off"),
        (
            {
                **FILE_LOCKED,
                "fileLockConfiguration": {"isClientAuthorizedToRead": False},
            },
            WRITER,
            "lock unreadable",
        ),
        (_bucket(retention=SEVEN_DAYS), WRITER, "default retention set"),
        (FILE_LOCKED, [*WRITER, "bypassGovernance"], "key bypasses governance"),
        ({"bucketName": "weekly"}, WRITER, "no lock config"),
        (
            _bucket(retention={**SEVEN_DAYS, "mode": "compliance"}),
            WRITER,
            "default retention set",
        ),
    ],
    ids=[
        "lock off",
        "unreadable",
        "governance default",
        "key bypasses governance",
        "no lock config",
        "compliance default",
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
        post_json=lambda url, payload, headers: {"buckets": [FILE_LOCKED]},
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
