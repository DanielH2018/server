"""Getting the bad news out: the Discord delivery queue.

An alert that fails to send has to survive to the next tick, and the queue has to stay bounded.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_health.py

from deploy_health import (
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
)


# The pending-alert queue reconciliation (gitops_deploy.deliver / drain_pending) is pure keep/drop
# logic lifted here so it's exercised without the un-importable deployer's discord() I/O. deliver()
# clears a key on a confirmed send and (re)queues its content on a failure; drain() drops only the
# entries a redelivery confirmed. A regression here silently drops (or never clears) a post-merge alert.
def test_apply_send_result_clears_key_on_delivery():
    assert apply_send_result({"secrets:abc": "msg"}, "secrets:abc", "msg", True) == {}


def test_apply_send_result_keeps_other_keys_on_delivery():
    pending = {"secrets:abc": "m1", "tasks:def": "m2"}
    assert apply_send_result(pending, "secrets:abc", "m1", True) == {"tasks:def": "m2"}


def test_apply_send_result_queues_content_on_failure():
    assert apply_send_result({}, "secrets:abc", "msg", False) == {"secrets:abc": "msg"}


def test_apply_send_result_requeues_updated_content_on_failure():
    # A re-detected alert with fresh content overwrites the stale queued copy.
    assert apply_send_result({"broad:abc": "old"}, "broad:abc", "new", False) == {
        "broad:abc": "new"
    }


def test_apply_send_result_delivery_of_absent_key_is_noop():
    # Delivering a key that was never queued leaves the queue unchanged (caller skips the write).
    pending = {"tasks:def": "m2"}
    assert apply_send_result(pending, "secrets:abc", "m1", True) == {"tasks:def": "m2"}


def test_apply_send_result_does_not_mutate_input():
    pending = {"secrets:abc": "msg"}
    apply_send_result(pending, "secrets:abc", "msg", True)
    assert pending == {"secrets:abc": "msg"}


def test_apply_drain_result_removes_only_delivered():
    pending = {"a:1": "x", "b:2": "y", "c:3": "z"}
    assert apply_drain_result(pending, {"a:1", "c:3"}) == {"b:2": "y"}


def test_apply_drain_result_none_delivered_keeps_all():
    pending = {"a:1": "x", "b:2": "y"}
    assert apply_drain_result(pending, set()) == pending


def test_apply_drain_result_all_delivered_empties():
    assert apply_drain_result({"a:1": "x"}, {"a:1"}) == {}


# ── the pending-alert queue is bounded ─────────────────────────────────────────────────────────


def test_cap_pending_leaves_a_queue_under_the_cap_alone():
    """The accepting half.

    A cap that trimmed unconditionally would pass the rejecting test below while quietly discarding
    alerts that fit.
    """
    queue = {f"secrets:{i}": "msg" for i in range(5)}
    capped, dropped = cap_pending(dict(queue), max_entries=64)
    assert capped == queue
    assert dropped == []


def test_cap_pending_evicts_the_oldest_first():
    """The rejecting half.

    Dicts preserve insertion order and json.load preserves it on the way back in, so the queue's own
    order IS its age order — no timestamps needed.
    """
    queue = {f"secrets:{i}": f"msg{i}" for i in range(6)}
    capped, dropped = cap_pending(queue, max_entries=4)
    assert dropped == ["secrets:0", "secrets:1"]
    assert list(capped) == ["secrets:2", "secrets:3", "secrets:4", "secrets:5"]
    assert capped["secrets:5"] == "msg5"


def test_cap_pending_reports_every_key_it_drops():
    """Dropping an undelivered alert without a trace is the failure the queue exists to prevent,
    one level up — so the caller must be able to log each one."""
    queue = {f"secrets:{i}": "msg" for i in range(10)}
    capped, dropped = cap_pending(queue, max_entries=3)
    assert len(dropped) == 7
    assert set(dropped).isdisjoint(capped)
    assert len(capped) == 3


def test_cap_pending_at_exactly_the_cap_drops_nothing():
    """The off-by-one that would silently discard one alert per tick at steady state."""
    queue = {f"secrets:{i}": "msg" for i in range(4)}
    capped, dropped = cap_pending(queue, max_entries=4)
    assert dropped == []
    assert capped == queue


def test_the_default_cap_is_the_one_the_deployer_uses():
    """Binds the default to the exported constant, so raising one raises both."""
    queue = {f"secrets:{i}": "msg" for i in range(PENDING_ALERTS_MAX + 2)}
    _capped, dropped = cap_pending(queue)
    assert len(dropped) == 2
