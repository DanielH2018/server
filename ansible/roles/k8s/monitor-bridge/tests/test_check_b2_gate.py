"""The B2-reachability gate: the throttled probe, and which failure holds the cache how long.

Split out of test_check_gates.py, which the module-length ratchet caps — the B2 arm is
self-contained (its own probe cache, its own two cache TTLs) and reads better beside the
incident it exists for than in the middle of the run-loop wiring tests.

WHAT THIS ARM IS FOR: on 2026-08-02 B2 refused every request for nine and a half hours while
the kopia-era state-file checks read green, because they reported their last successful cron
run rather than current B2 health. `b2_reachable` probes B2 itself, and caches the outcome so
that watching a TRANSACTION cap does not spend it — but caches a transport failure for only
one cycle, because a connection that never landed was never billed.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_b2_gate.py
"""

import email.message
import urllib.error
from dataclasses import replace

import pytest

import bridge.net
import checks.b2
import check
from _check_gate_helpers import mk
from bridge.types import Check
from gates import Gates


def _reset_b2_probe(
    cfg, monkeypatch, key_id="kid", app_key="akey", interval=1800, transport_retry=300
):
    cfg = replace(
        cfg,
        B2_PROBE_KEY_ID=key_id,
        B2_PROBE_APPLICATION_KEY=app_key,
        B2_PROBE_INTERVAL_S=interval,
        B2_TRANSPORT_RETRY_S=transport_retry,
    )
    monkeypatch.setattr(
        checks.b2,
        "_b2_probe",
        {"ts": 0.0, "ok": True, "msg": "not yet probed", "ttl": interval},
    )
    return cfg


def _cap_denial(cfg):
    """The exception a real transaction-cap breach raises.

    _get_json re-raises urllib HTTPError UNTOUCHED (check.py, "Re-raise the SAME type") and wraps
    only non-HTTP failures as RuntimeError. b2_reachable now branches on exactly that distinction
    to pick a cache TTL, so a test that fakes a 403 as a RuntimeError would exercise the transport
    path and prove the opposite of what it claims. These tests used to do that.
    """
    err = urllib.error.HTTPError(
        cfg.B2_PROBE_URL,
        403,
        "Forbidden: transaction_cap_exceeded",
        email.message.Message(),
        None,
    )
    # Closed, as _get_json leaves a real one: an open HTTPError is a ResourceWarning at GC,
    # which filterwarnings=error fails whichever test happens to be running then.
    err.close()
    return err


def test_b2_reachable_disabled_without_credentials(monkeypatch, cfg):
    cfg = _reset_b2_probe(cfg, monkeypatch, key_id="", app_key="")
    ok, msg = checks.b2.b2_reachable(cfg, now=10_000)
    assert ok is True and "disabled" in msg


@pytest.mark.parametrize(
    ("response", "ok", "must_contain"),
    [
        pytest.param({"accountId": "a1"}, True, ("reachable",), id="ok_on_account_id"),
        # Version-tolerant: Backblaze publishes a v4 body example (accountId top-level) but none
        # for v3, so either field proves it's B2. Pinning one shape would page every cycle if it
        # moved.
        pytest.param(
            {"authorizationToken": "t"},
            True,
            (),
            id="accepts_authorization_token_only",
        ),
        # A 200 from something that isn't B2 must not read as healthy.
        pytest.param(
            {"unexpected": 1}, False, ("accountId",), id="rejects_unrecognised_response"
        ),
    ],
)
def test_b2_authorize(monkeypatch, response, ok, must_contain, cfg):
    cfg = replace(cfg, B2_PROBE_KEY_ID="kid", B2_PROBE_APPLICATION_KEY="akey")
    monkeypatch.setattr(bridge.net, "_get_json", lambda url, headers=None: response)
    result_ok, msg = checks.b2.b2_authorize(cfg)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_b2_reachable_surfaces_the_cap_error_text(monkeypatch, cfg):
    # G3: the alert must name the CAUSE. B2 answers a cap breach with transaction_cap_exceeded,
    # and _get_json appends the response body to the HTTPError, so it has to reach the message.
    cfg = _reset_b2_probe(cfg, monkeypatch)

    def _boom(url, headers=None):
        raise _cap_denial(cfg)

    monkeypatch.setattr(bridge.net, "_get_json", _boom)
    ok, msg = checks.b2.b2_reachable(cfg, now=10_000)
    assert ok is False and "transaction_cap_exceeded" in msg


def test_b2_reachable_caches_failure_and_does_not_reprobe(monkeypatch, cfg):
    # THE cost-critical property. The fault being detected is a transaction cap, so a failure must
    # NOT re-probe every cycle the way email_backstop does — that would spend the exhausted budget.
    cfg = _reset_b2_probe(cfg, monkeypatch)
    calls = []

    def _boom(url, headers=None):
        calls.append(url)
        raise _cap_denial(cfg)

    monkeypatch.setattr(bridge.net, "_get_json", _boom)
    first_ok, _ = checks.b2.b2_reachable(cfg, now=10_000)
    # five more cycles inside the interval (INTERVAL=300 -> 25 min of cycles)
    for offset in (300, 600, 900, 1200, 1500):
        ok, msg = checks.b2.b2_reachable(cfg, now=10_000 + offset)
        assert ok is False
        assert (
            "transaction_cap_exceeded" in msg
        )  # cached verdict still reported every cycle
    assert first_ok is False
    assert len(calls) == 1, "a cached failure must not re-probe: %d calls" % len(calls)


def test_b2_reachable_reprobes_a_transport_failure_next_cycle(monkeypatch, cfg):
    # The REJECT half of the caching pair above, and the 2026-08-30 restart's fix. A failure that
    # never reached B2 was billed nothing, so the cost argument that justifies the 30-minute cache
    # does not apply to it — and holding it pinned the gate DOWN for 25 minutes against an 8m35s
    # outage, because the cache was holding back the RECOVERY as well as the retry.
    cfg = _reset_b2_probe(cfg, monkeypatch, interval=1800, transport_retry=300)
    calls = []
    # _get_json wraps DNS/connect/timeout failures as RuntimeError; only these take the short TTL.
    outcomes = [RuntimeError("b2 api: Temporary failure in name resolution")]

    def _flaky(url, headers=None):
        calls.append(url)
        if outcomes:
            raise outcomes.pop(0)
        return {"accountId": "a1"}

    monkeypatch.setattr(bridge.net, "_get_json", _flaky)
    ok, msg = checks.b2.b2_reachable(cfg, now=10_000)
    assert ok is False and "name resolution" in msg
    # One cycle later the transport TTL has expired, so the gate re-probes and recovers — where a
    # cap denial would still be reporting its cached verdict for another 25 minutes.
    ok, msg = checks.b2.b2_reachable(cfg, now=10_300)
    assert ok is True, "a transport failure must re-probe next cycle, got: %s" % msg
    assert len(calls) == 2, "expected a re-probe, got %d calls" % len(calls)


def test_b2_transport_retry_is_shorter_than_the_probe_interval(cfg):
    # The two TTLs must not converge: if the transport retry ever reached B2_PROBE_INTERVAL_S the
    # split above would be a no-op that still reads as implemented.
    assert cfg.B2_TRANSPORT_RETRY_S < cfg.B2_PROBE_INTERVAL_S


def test_b2_reachable_reprobes_after_the_interval(monkeypatch, cfg):
    cfg = _reset_b2_probe(cfg, monkeypatch, interval=1800)
    calls = []

    def _ok(url, headers=None):
        calls.append(url)
        return {"accountId": "a1"}

    monkeypatch.setattr(bridge.net, "_get_json", _ok)
    checks.b2.b2_reachable(cfg, now=10_000)
    checks.b2.b2_reachable(cfg, now=10_000 + 1799)  # still cached
    assert len(calls) == 1
    checks.b2.b2_reachable(cfg, now=10_000 + 1801)  # interval elapsed
    assert len(calls) == 2


def _wire_run_once_b2(cfg, monkeypatch, b2_result, checks, b2_dependent):
    """Drive run_once with Prometheus+Loki UP and a stated B2-reachability result."""
    ran, pushes = [], []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, t, ok, m: pushes.append((t, ok, m))
    )
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, q: [])
    check.run_once(
        cfg,
        [Check(n, "tok_%s" % n, mk(ran, n)) for n in checks],
        gates=Gates(
            prom_dependent=frozenset(),
            loki_dependent=frozenset(),
            startup_grace=frozenset(),
            b2_dependent=frozenset(b2_dependent),
            probe_prometheus=lambda _cfg: (True, "prom ok"),
            probe_loki=lambda _cfg: (True, "loki ok"),
            probe_b2=lambda _cfg: b2_result,
        ),
    )
    return ran, pushes


def test_run_once_suppresses_b2_dependent_when_b2_down(monkeypatch, cfg):
    ran, pushes = _wire_run_once_b2(
        cfg,
        monkeypatch,
        (False, "B2 unreachable: HTTP Error 403: transaction_cap_exceeded"),
        ["b2_usage", "verify", "backup"],
        {"b2_usage", "verify"},
    )
    # The four state-file checks stop reporting their last-successful-run as current health...
    assert not ({"b2_usage", "verify"} & set(ran))
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_b2_usage"][0] is True
    assert "b2" in by_tok["tok_b2_usage"][1].lower()
    # ...while Backup Freshness still runs and can page — it is the signal that was right.
    assert "backup" in ran
    assert any(
        ok is False and "transaction_cap_exceeded" in m for _, ok, m in pushes
    ), "the B2 Reachable monitor must page with B2's own error text"


def test_run_once_runs_b2_dependent_when_b2_up(monkeypatch, cfg):
    ran, _ = _wire_run_once_b2(
        cfg,
        monkeypatch,
        (True, "B2 reachable"),
        ["b2_usage", "verify"],
        {"b2_usage", "verify"},
    )
    assert "b2_usage" in ran and "verify" in ran
