"""The B2 Object Lock and R2 etcd lock rows ADR-0041 adds."""

import calendar
import email.message
import urllib.error
import urllib.parse
from dataclasses import replace
from datetime import date, timedelta

import pytest

import checks.b2
import checks.r2
from _fake_sources import FakeSources
from bridge.types import CheckResult
from verdicts.storage import (
    R2_CANARY_PREFIX,
    CanaryPlan,
    b2_lock_verdict,
    r2_canary_plan,
)

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
            "allowed": {
                "buckets": [{"id": "b1", "name": "weekly"}],
                "capabilities": WRITER,
            },
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
        "https://api/b2api/v4/b2_list_buckets",
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


TODAY = date(2026, 11, 23)
NOON = calendar.timegm((2026, 11, 23, 12, 0, 0))


def _canary(day: date) -> str:
    return R2_CANARY_PREFIX + day.isoformat()


def test_a_first_run_uploads_today_and_probes_it():
    assert r2_canary_plan([], TODAY, 7) == CanaryPlan(
        upload=_canary(TODAY), probe=_canary(TODAY), expired=()
    )


def test_the_plan_probes_the_newest_locked_canary_and_expires_those_past_the_lock():
    keys = [_canary(TODAY - timedelta(days=n)) for n in (0, 1, 3, 6, 7, 8, 10)]
    keys.append(R2_CANARY_PREFIX + "not-a-date")
    assert r2_canary_plan(keys, TODAY, 7) == CanaryPlan(
        upload=None,
        probe=_canary(TODAY - timedelta(days=1)),
        expired=(
            _canary(TODAY - timedelta(days=10)),
            _canary(TODAY - timedelta(days=8)),
        ),
    )


class FakeR2:
    """A bucket whose lock refuses each delete or overwrite of a key in `locked`."""

    def __init__(self, keys: list[str], locked: set[str]):
        self.keys = list(keys)
        self.locked = locked
        self.puts: list[tuple[str, bytes | None]] = []

    def send(self, method, url, headers, body=None):
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=kid/")
        key = urllib.parse.urlsplit(url).path.removeprefix("/etcd-r2/")
        if method == "GET":
            return "".join("<Key>%s</Key>" % k for k in self.keys).encode()
        if key in self.locked and (method == "DELETE" or key in self.keys):
            raise _http_error(url, 409, "<Code>ObjectLockedByBucketPolicy</Code>")
        if method == "PUT":
            self.puts.append((key, body))
            self.keys.append(key)
            return b""
        self.keys.remove(key)
        return b""


def _http_error(url, code, msg):
    err = urllib.error.HTTPError(url, code, msg, email.message.Message(), None)
    err.close()
    return err


@pytest.fixture
def r2_cfg(cfg):
    return replace(
        cfg,
        CF_ACCOUNT_ID="acct",
        R2_BUCKET="etcd-r2",
        R2_S3_KEY_ID="kid",
        R2_S3_SECRET_KEY="secret",
    )


def _run(r2_cfg, bucket, now=NOON, state=None):
    src = FakeSources(state=state, send=bucket.send)
    return checks.r2.check_r2_etcd_lock(r2_cfg, src, now=now), src


def test_a_refused_delete_of_yesterdays_canary_is_clean(r2_cfg):
    # fact: k8s/roles/monitor-bridge/CLAUDE.md#Autonomous-role contract (the one row that writes)
    yesterday = _canary(TODAY - timedelta(days=1))
    bucket = FakeR2([yesterday], locked={yesterday, _canary(TODAY)})
    (ok, msg), _ = _run(r2_cfg, bucket)
    assert ok, msg
    assert bucket.puts == [(_canary(TODAY), b"")]


def test_a_canary_r2_deletes_inside_the_lock_is_flagged(r2_cfg):
    yesterday = _canary(TODAY - timedelta(days=1))
    result, _ = _run(r2_cfg, FakeR2([yesterday], locked=set()))
    assert not result.ok
    assert result.details == ("lock missing",)


def test_a_canary_past_the_lock_that_r2_refuses_to_delete_is_flagged(r2_cfg):
    old = _canary(TODAY - timedelta(days=9))
    result, _ = _run(r2_cfg, FakeR2([old], locked={old, _canary(TODAY)}))
    assert not result.ok
    assert result.details == ("lock outlasts its age",)


def test_a_second_run_the_same_day_neither_uploads_nor_reads_r2(r2_cfg):
    bucket = FakeR2([], locked={_canary(TODAY)})
    (ok, _), src = _run(r2_cfg, bucket)
    assert ok
    (ok, msg), later = _run(r2_cfg, bucket, now=NOON + 3600, state=src.state)
    assert ok and "checked" in msg
    assert later.calls == []
    assert len(bucket.puts) == 1


def test_a_restarted_bridge_does_not_upload_a_canary_r2_already_holds(r2_cfg):
    (ok, msg), _ = _run(r2_cfg, FakeR2([_canary(TODAY)], locked={_canary(TODAY)}))
    assert ok, msg


def test_any_other_r2_refusal_is_flagged(r2_cfg):
    class Forbidden(FakeR2):
        def send(self, method, url, headers, body=None):
            raise _http_error(url, 403, "<Code>AccessDenied</Code>")

    result, _ = _run(r2_cfg, Forbidden([], locked=set()))
    assert not result.ok
    assert result.details == ("request refused",)


def test_an_r2_server_error_raises_and_the_next_cycle_probes_again(r2_cfg):
    class Unavailable(FakeR2):
        def send(self, method, url, headers, body=None):
            raise _http_error(url, 503, "<Code>ServiceUnavailable</Code>")

    src = FakeSources(send=Unavailable([], locked=set()).send)
    with pytest.raises(urllib.error.HTTPError):
        checks.r2.check_r2_etcd_lock(r2_cfg, src, now=NOON)
    bucket = FakeR2([], locked={_canary(TODAY)})
    (ok, msg), _ = _run(r2_cfg, bucket, now=NOON + 300, state=src.state)
    assert ok, msg
