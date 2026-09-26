"""The keyed secret-manifest digest must move with the content and leak nothing without the key.

`files/secret_hmac.py` is what release_digest.yml runs, one call per secret file, for the
`secret_digest` both records carry (#2574). The staleness reader clears a service with secret
manifests only when the two records' digests match, so a digest that failed to move would
hide a real change. The uptime-kuma monitor added to `static-monitors.yaml` on 2026-09-25 is
the case that motivated it.

Run: uv run pytest ansible/roles/k8s/manifests/tests
"""

import hashlib
import os
import stat
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

import secret_hmac

MONITORS = b"monitors:\n  - name: sonarr\n    password: hunter2\n"
ONE_MORE = MONITORS + b"  - name: radarr\n    password: hunter2\n"


def _key(tmp_path):
    key = tmp_path / "key"
    assert secret_hmac.ensure_key(str(key)) == "created"
    return key


def _digest(key, tmp_path, content):
    manifest = tmp_path / "static-monitors.yaml"
    manifest.write_bytes(content)
    return secret_hmac.digest(str(key), str(manifest))


def test_the_digest_moves_when_a_secret_manifest_changes(tmp_path):
    key = _key(tmp_path)
    before = _digest(key, tmp_path, MONITORS)
    assert _digest(key, tmp_path, MONITORS) == before
    assert _digest(key, tmp_path, ONE_MORE) != before


def test_the_digest_is_keyed_not_a_plain_hash(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    first = _digest(_key(first), tmp_path, MONITORS)
    second = _digest(_key(second), tmp_path, MONITORS)
    assert first != second
    assert hashlib.sha256(MONITORS).hexdigest() not in (first, second)


def test_the_key_is_created_once_and_private(tmp_path):
    key = _key(tmp_path)
    original = key.read_bytes()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert len(original) == secret_hmac.KEY_BYTES
    assert secret_hmac.ensure_key(str(key)) == "present"
    assert key.read_bytes() == original


def test_a_missing_key_and_a_missing_file_exit_differently(tmp_path, capsys):
    key = _key(tmp_path)
    assert secret_hmac.main([str(tmp_path / "nokey"), str(key)]) == 2
    assert secret_hmac.main([str(key), str(tmp_path / "nofile")]) == 1
    assert capsys.readouterr().out == ""
