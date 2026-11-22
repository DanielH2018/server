"""The B2 Object Lock sweep, driven through the real B2Session over a fake transport."""

from backup import b2_extend_retention as sweep
from lib.b2 import B2Error, B2Session

NOW = 1_800_000_000
PREFIX = sweep.BACKUPSTORE_PREFIX
BLOCK = f"{PREFIX}a1/b2/pvc-a/blocks/aa/bb/deadbeef.blk"
CFG = f"{PREFIX}a1/b2/pvc-a/backups/backup_backup-1.cfg"
LOCK_FILE = f"{PREFIX}a1/b2/pvc-a/locks/lock-1.lck"


def _auth(capabilities=("listFiles", "writeFileRetentions")):
    return {
        "apiInfo": {
            "storageApi": {
                "apiUrl": "https://api.example",
                "allowed": {
                    "buckets": [{"id": "bid", "name": "weekly"}],
                    "capabilities": list(capabilities),
                },
            }
        },
        "authorizationToken": "tok",
        "accountId": "acct",
    }


NO_DEFAULT = {"mode": None, "period": None}


def _bucket(enabled=True, retention=NO_DEFAULT):
    return {
        "buckets": [
            {
                "bucketName": "weekly",
                "fileLockConfiguration": {
                    "isClientAuthorizedToRead": True,
                    "value": {
                        "isFileLockEnabled": enabled,
                        "defaultRetention": retention,
                    },
                },
            }
        ]
    }


def _listing(*names):
    return {"files": [{"fileName": n, "fileId": f"id-{n}"} for n in names]}


class FakeTransport:
    """Answers each call from a queue, or with `update` for every retention update."""

    def __init__(self, *responses, update=None):
        self.responses = list(responses)
        self.update = update or (lambda payload: {})
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        if url.endswith("/b2_update_file_retention"):
            return self.update(payload)
        return self.responses.pop(0)

    def updates(self):
        return [p for api, p in self.calls if api == "b2_update_file_retention"]


def _run(transport, capsys):
    code = sweep.main(
        [],
        _secret=lambda key: f"secret-{key}",
        _session=lambda k, a: B2Session(k, a, transport=transport),
        _now=lambda: NOW,
    )
    out = capsys.readouterr()
    return code, out.out.strip(), out.err.strip()


def test_a_file_lock_bucket_with_no_default_has_every_object_but_a_lock_file_locked(
    capsys,
):
    transport = FakeTransport(_auth(), _bucket(), _listing(BLOCK, CFG, LOCK_FILE))
    code, out, _ = _run(transport, capsys)
    until_ms = (NOW + 7 * 86400) * 1000
    assert code == 0
    assert transport.updates() == [
        {
            "fileName": name,
            "fileId": f"id-{name}",
            "fileRetention": {"mode": "governance", "retainUntilTimestamp": until_ms},
        }
        for name in (BLOCK, CFG)
    ]
    assert out.startswith("b2-retention: extended 2 of 2 objects to 2027-01-22T")
    assert out.endswith("0 gone, 3 Class C")


def test_the_period_is_the_sweeps_own_whatever_the_bucket_default():
    one_day = {"mode": "governance", "period": {"duration": 1, "unit": "days"}}
    transport = FakeTransport(_auth(), _bucket(retention=one_day), _listing(BLOCK))
    sweep.run(B2Session("k", "s", transport=transport), NOW)
    until_ms = (NOW + sweep.RETENTION_DAYS * 86400) * 1000
    assert [
        u["fileRetention"]["retainUntilTimestamp"] for u in transport.updates()
    ] == [until_ms]


def test_the_listing_is_scoped_to_the_backupstore_volumes():
    transport = FakeTransport(_auth(), _bucket(), _listing())
    sweep.run(B2Session("k", "s", transport=transport), NOW)
    assert transport.calls[-1] == (
        "b2_list_file_names",
        {"bucketId": "bid", "prefix": PREFIX, "maxFileCount": 1000},
    )


def test_a_key_without_write_file_retentions_is_blocked_on_a_locked_bucket(capsys):
    transport = FakeTransport(_auth(capabilities=("listFiles",)), _bucket())
    code, out, _ = _run(transport, capsys)
    assert code == 1
    assert out == "b2-retention: blocked: the key lacks writeFileRetentions"
    assert "b2_list_file_names" not in [api for api, _ in transport.calls]


def test_a_bucket_with_file_lock_off_declines_before_the_key_is_judged(capsys):
    transport = FakeTransport(
        _auth(capabilities=("listFiles",)), _bucket(enabled=False)
    )
    code, out, _ = _run(transport, capsys)
    assert code == 0
    assert out == "b2-retention: declined: Object Lock is off on the bucket"
    assert "b2_list_file_names" not in [api for api, _ in transport.calls]


def test_an_object_a_prune_removed_after_the_listing_counts_as_gone(capsys):
    def update(payload):
        if payload["fileName"] == BLOCK:
            raise B2Error("HTTP 400: file_not_present", code="file_not_present")
        return {}

    transport = FakeTransport(_auth(), _bucket(), _listing(BLOCK, CFG), update=update)
    code, out, _ = _run(transport, capsys)
    assert code == 0
    assert "extended 1 of 2 objects" in out
    assert "1 gone" in out


def test_any_other_refusal_fails_the_run_and_names_the_object(capsys):
    def update(payload):
        raise B2Error("HTTP 401: unauthorized", code="unauthorized")

    transport = FakeTransport(_auth(), _bucket(), _listing(BLOCK, CFG), update=update)
    code, out, _ = _run(transport, capsys)
    assert code == 1
    assert "extended 0 of 2 objects" in out
    assert f"FAILED 2: {BLOCK}: HTTP 401: unauthorized" in out


def test_a_failed_listing_errors_without_a_summary_line(capsys):
    def transport(url, headers, payload, timeout):
        if url.endswith("/b2_list_file_names"):
            raise B2Error("HTTP 403 from b2_list_file_names: unauthorized")
        return _auth() if "authorize" in url else _bucket()

    code = sweep.main(
        [],
        _secret=lambda key: "s",
        _session=lambda k, a: B2Session(k, a, transport=transport),
        _now=lambda: NOW,
    )
    out = capsys.readouterr()
    assert code == 75
    assert out.out == ""
    assert out.err.startswith("b2-retention: error: HTTP 403")


def test_an_update_b2_never_answered_stops_the_sweep(capsys):
    def update(payload):
        raise B2Error("B2 request failed: timed out")

    transport = FakeTransport(_auth(), _bucket(), _listing(BLOCK, CFG), update=update)
    code, out, err = _run(transport, capsys)
    assert code == 75
    assert out == ""
    assert err == "b2-retention: error: B2 request failed: timed out"
    assert len(transport.updates()) == 1
