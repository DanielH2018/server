"""`hosts/lib/homelab-secrets` step 5: the HMAC baseline, `pending.json` and its lock.

Each case is one of H4's *Verification* cases for the restart queue. Where a rule has a way to
break it, the case is a pair: the run the design asks for, and a run with the state removed or
damaged that must behave the other way. The tree and the stub `sops` come from _secrets_host.py.

Run: uv run pytest hosts/lib/tests/test_homelab_secrets_queue.py
"""

import fcntl
import json
import shutil
import threading

import pytest
from _secrets_host import Host

PENDING = "/run/homelab-secrets/pending.json"
LOCK = "/run/homelab-secrets/pending.lock"
DIGESTS = "/var/lib/homelab-secrets/.digests"
KEY = "/var/lib/homelab-secrets/.hmac-key"


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


def queue(host):
    path = host.path(PENDING)
    return json.loads(path.read_text()) if path.exists() else []


def two_outputs(host, **values):
    host.decrypts_to(**values)
    host.output(
        "etc/nut/upsmon.conf",
        "pw=${nut_pw}\n",
        persist=True,
        keys=["nut_pw"],
        dependents=["nut-monitor.service"],
    )
    host.output(
        "app/env",
        "T=${tok}\n",
        keys=["tok"],
        dependents=["app.service", "app-sidecar.service"],
    )


def drain(host):
    host.path(PENDING).unlink()


def test_a_first_run_queues_every_dependent_and_records_the_state(host):
    two_outputs(host, nut_pw="pw1", tok="t1")

    assert host.run() == 0

    assert queue(host) == ["app-sidecar.service", "app.service", "nut-monitor.service"]
    for state in (DIGESTS, KEY):
        assert host.mode(state) == 0o600
        name = state.rsplit("/", 1)[1]
        assert [c[1:] for c in host.chowns if f"/{'.' + name}." in c[0]] == [
            ("root", "root")
        ]
    assert host.mode(PENDING) == 0o644
    assert host.mode(LOCK) == 0o600
    assert len(host.path(KEY).read_bytes()) == 32
    assert sorted(json.loads(host.read(DIGESTS))) == [
        "/run/homelab-secrets/app/env",
        "/var/lib/homelab-secrets/etc/nut/upsmon.conf",
    ]
    for state in (DIGESTS, KEY, PENDING):
        assert b"pw1" not in host.path(state).read_bytes()
        assert b"t1" not in host.path(state).read_bytes()


def test_an_unchanged_value_adds_nothing(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    drain(host)

    assert host.run() == 0

    assert queue(host) == []


@pytest.mark.parametrize("lost", [DIGESTS, KEY], ids=["digests", "hmac-key"])
def test_with_no_usable_baseline_every_dependent_is_added(host, lost):
    # The reject half of the pair above: the same values, but the baseline is gone.
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    drain(host)
    host.path(lost).unlink()

    assert host.run() == 0

    assert len(queue(host)) == 3


def test_a_run_after_a_reboot_adds_nothing(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    shutil.rmtree(host.path("/run"))  # a reboot empties /run and keeps /var/lib

    assert host.run() == 0

    assert queue(host) == []
    assert host.read("/run/homelab-secrets/app/env") == "T=t1\n"


def test_a_changed_value_adds_only_its_own_dependents_and_keeps_the_pending_entries(
    host,
):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    host.path(PENDING).write_text(json.dumps(["zz-old.service"]))
    host.decrypts_to(nut_pw="pw1", tok="t2")

    assert host.run() == 0

    assert queue(host) == ["app-sidecar.service", "app.service", "zz-old.service"]


def test_a_failed_output_queues_nothing_and_keeps_its_baseline_for_the_next_run(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    drain(host)
    before = json.loads(host.read(DIGESTS))
    host.decrypts_to(nut_pw="pw1")  # `tok` is gone: app/env fails

    assert host.run() == 1

    assert queue(host) == []
    assert json.loads(host.read(DIGESTS)) == before
    host.decrypts_to(nut_pw="pw1", tok="t2")
    assert host.run() == 0
    assert queue(host) == ["app-sidecar.service", "app.service"]


def test_a_failed_decrypt_leaves_the_queue_and_the_baseline_alone(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    state = {a: host.path(a).read_bytes() for a in (PENDING, DIGESTS, KEY)}
    host.plain.unlink()

    assert host.run() == 1

    assert {a: host.path(a).read_bytes() for a in state} == state


def test_an_output_no_longer_listed_loses_its_baseline_and_keeps_its_queued_units(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.run()
    host.outputs.pop()  # app/env leaves the manifest

    assert host.run() == 0

    assert list(json.loads(host.read(DIGESTS))) == [
        "/var/lib/homelab-secrets/etc/nut/upsmon.conf"
    ]
    assert "app.service" in queue(host)


def test_a_host_with_no_outputs_still_gets_the_baseline_files(host):
    host.decrypts_to(k="v")

    assert host.run() == 0

    assert json.loads(host.read(DIGESTS)) == {}
    assert len(host.path(KEY).read_bytes()) == 32
    assert queue(host) == []


def hold_lock(host):
    """Hold pending.lock from another open file description, as reconcile would."""
    host.path(LOCK).parent.mkdir(parents=True, exist_ok=True)
    handle = open(host.path(LOCK), "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def test_the_script_waits_for_the_lock_and_then_writes(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    handle = hold_lock(host)
    timer = threading.Timer(0.3, handle.close)
    timer.start()

    assert host.run() == 0
    timer.join()

    assert len(queue(host)) == 3


def test_a_lock_held_past_the_timeout_fails_the_run_and_writes_no_state(host):
    # The reject half of the pair above: the holder keeps the lock until the run has returned.
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.module.LOCK_TIMEOUT_SECONDS = 0.2
    handle = hold_lock(host)

    try:
        assert host.run() == 1
    finally:
        handle.close()

    assert not host.path(PENDING).exists()
    assert not host.path(DIGESTS).exists()
    assert host.read("/run/homelab-secrets/app/env") == "T=t1\n"


def test_an_unreadable_queue_fails_the_run_and_records_no_baseline(host):
    two_outputs(host, nut_pw="pw1", tok="t1")
    host.path(PENDING).parent.mkdir(parents=True)
    host.path(PENDING).write_text("{not json")

    assert host.run() == 1

    assert host.read(PENDING) == "{not json"
    assert not host.path(DIGESTS).exists()
