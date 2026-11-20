"""The two bucket-lock rows ADR-0041 adds: Object Lock on B2, and the etcd lock rule on R2."""

import email.message
import urllib.error
from dataclasses import replace

import pytest

import checks.b2
import checks.r2
from _fake_sources import FakeSources
from bridge.types import CheckResult
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from verdicts.storage import (
    R2_ETCD_PREFIX,
    R2_LONGHORN_PREFIX,
    b2_lock_verdict,
    r2_lock_verdict,
)

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
WRITER = ["listBuckets", "writeFiles", "deleteFiles", "readBucketRetentions"]
ETCD_RULE = {
    "id": "etcd-7d",
    "enabled": True,
    "prefix": "etcd-snapshots/",
    "condition": {"type": "Age", "maxAgeSeconds": 604800},
}
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
        ({"bucketName": "weekly"}, WRITER, "no lock config"),
    ],
    ids=[
        "lock off",
        "unreadable",
        "no default retention",
        "key bypasses governance",
        "no lock config",
    ],
)
def test_a_bucket_that_does_not_bind_the_writer_is_flagged(
    bucket, capabilities, detail
):
    result = CheckResult.of(b2_lock_verdict(bucket, capabilities))
    assert not result.ok
    assert result.details == (detail,)


def test_an_age_rule_over_the_etcd_prefix_alone_is_clean():
    ok, msg = r2_lock_verdict([ETCD_RULE])
    assert ok
    assert "7 days" in msg


@pytest.mark.parametrize(
    "rules, detail",
    [
        ([{**ETCD_RULE, "enabled": False}], "etcd snapshots unlocked"),
        (
            [{**ETCD_RULE, "condition": {"type": "Indefinite"}}],
            "etcd snapshots unlocked",
        ),
        ([{**ETCD_RULE, "prefix": ""}], "lock over the Longhorn tier"),
        (
            [ETCD_RULE, {**ETCD_RULE, "id": "lh", "prefix": "longhorn/backupstore/"}],
            "lock over the Longhorn tier",
        ),
    ],
    ids=[
        "disabled",
        "indefinite",
        "whole bucket",
        "inside the Longhorn prefix",
    ],
)
def test_a_lock_that_misses_etcd_or_reaches_longhorn_is_flagged(rules, detail):
    result = CheckResult.of(r2_lock_verdict(rules))
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


@pytest.fixture
def r2_cfg(cfg):
    return replace(cfg, CF_ACCOUNT_ID="acct", CF_ANALYTICS_TOKEN="tok", R2_BUCKET="bkt")


def test_the_r2_row_reads_the_buckets_lock_rules(r2_cfg):
    src = FakeSources(
        get_json=lambda url, headers: {
            "success": True,
            "result": {"rules": [ETCD_RULE]},
        }
    )
    ok, _ = checks.r2.check_r2_etcd_lock(r2_cfg, src, now=1000.0)
    assert ok
    assert src.queries("get_json") == [
        "https://api.cloudflare.com/client/v4/accounts/acct/r2/buckets/bkt/lock"
    ]


def test_an_r2_read_cloudflare_refuses_raises_rather_than_reading_as_unlocked(r2_cfg):
    src = FakeSources(
        get_json=lambda url, headers: {"success": False, "errors": [{"code": 10000}]}
    )
    with pytest.raises(RuntimeError, match="refused"):
        checks.r2.check_r2_etcd_lock(r2_cfg, src, now=1000.0)
    assert src.state.r2_lock_probe["ts"] is None


def test_the_r2_prefixes_are_the_ones_k3s_and_longhorn_write_under():
    role = K8S_ROLES / "longhorn-backup"
    defaults = yaml_fast.safe_load((role / "defaults" / "main.yml").read_text())
    assert R2_LONGHORN_PREFIX == defaults["k3s_longhorn_r2_backup_prefix"] + "/"
    secret = (role / "templates" / "k3s-etcd-snapshot-s3-secret.yaml.j2").read_text()
    assert f"etcd-s3-folder: {R2_ETCD_PREFIX.rstrip('/')}\n" in secret
