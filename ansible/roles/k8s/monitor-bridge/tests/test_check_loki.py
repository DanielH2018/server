"""Loki: the selector roster, the log-error arm, and the ingestion watchdog.

A LogQL selector naming a label Alloy does not ship matches no stream and reports "no events"
forever. HA_BAN_SELECTOR shipped that way with app="home-assistant". A fail-open arm cannot tell
"nothing to report" from "wrong question", so the selector labels are checked against the
observed stream vocabulary rather than against the check's own verdict.

The ingestion watchdog counts lines for an always-active stream over a window and goes down at
zero — the freshness analogue of the SMART and restore-drill checks.
"""

import re

from dataclasses import fields, replace

import pytest

import bridge.config
import bridge.net
import checks.logs

from lib.repo_paths import REPO as _REPO
from _bridge_env import bridge_env
from _helpers import ALL_VARS, load_yaml

# ── HA ip_ban arm ───────────────────────────────────────────────────────────────────────────
# HA's ban middleware keys on the peer address, so a burst of bad /api/ calls can ban the node's
# pod-network gateway. The probes exec curl to 127.0.0.1 and are immune; this arm is what
# keeps the ban itself from being silent.
# Two limits, so this guard is not over-trusted:
#   1. It reads each selector off the `cfg` fixture, which since #3659 is the rendered
#      env-secret, so the deployed LOKI_STREAM is what is checked. A selector built at runtime
#      from other values is not.
#   2. The vocabulary below came from one k8s pod stream. LOKI_STREAM selects file-tail streams,
#      which may legitimately carry labels this set does not list. Widen the set against a live
#      stream if a genuine selector ever fails — do not delete the guard.
# The k8s pod stream vocabulary, read off a live Loki stream. `app` is NOT in it — a
# selector with app="home-assistant" matches no stream and reports "no ip_ban events" forever. A fail-open arm cannot tell "nothing to report" from "wrong question",
# so the selector label has to be checked by something other than the check's own verdict.
# Transcribed from a live stream. Kept as a FLOOR rather than the whole answer:
# `filename`, `stream` and `service_name` are added by Alloy/Loki itself and appear in no
# config, so deriving alone would under-count and reject a valid selector.
_LOKI_STREAM_LABELS_OBSERVED = frozenset(
    {
        "container",
        "filename",
        "job",
        "machine",
        "namespace",
        "pod",
        "service_name",
        "stream",
    }
)


def _alloy_relabel_targets():
    """`target_label = "…"` values from the Alloy config — the labels it actually sets.

    Derived rather than transcribed because the transcription cannot follow a rename: renaming a
    relabel target leaves the frozenset above listing a label nothing emits, so a selector using
    the NEW name is rejected while one using the dead name passes — the guard reporting the
    opposite of the truth. Internal `__foo__` labels are dropped; they never reach a stream.
    """
    cfg = (
        _REPO / "ansible/roles/k8s/loki-homelab/templates/config/config.alloy.j2"
    ).read_text()
    found = set(re.findall(r'target_label\s*=\s*"([^"]+)"', cfg))
    return {label for label in found if not label.startswith("__")}


LOKI_STREAM_LABELS = _LOKI_STREAM_LABELS_OBSERVED | _alloy_relabel_targets()


def test_the_alloy_config_is_actually_readable():
    """A path typo would make _alloy_relabel_targets() return an empty set, silently reducing
    the vocabulary to the transcribed floor and re-opening the gap this closes."""
    assert _alloy_relabel_targets() >= {"container", "pod", "namespace", "machine"}, (
        "the expected target_label values did not parse from the Alloy config — the path or "
        "the config shape changed, and the derived half of LOKI_STREAM_LABELS is now inert"
    )


def _selector_labels(selector):
    """Label names in a LogQL stream selector — the `foo` of `{foo="bar",baz=~"qux"}`."""
    head = selector.split("}", 1)[0]
    return set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", head))


def _logql_selector_names(cfg):
    """Every `Config` field that holds a LogQL stream selector, found by shape.

    Derived rather than listed. A hardcoded list would let a later selector, such as
    LOKI_PI_STREAM, join unchecked. A selector this cannot see is a selector that can
    name a label Alloy does not emit and go permanently green, which is the exact failure
    the test exists for.

    Matched on the LEADING `{...}` only, deliberately: a selector may carry line filters
    after the closing brace (`{...} |~ "Banned IP"`), and requiring the string to END in `}`
    silently drops HA_BAN_SELECTOR -- narrowing the roster while looking like it widened it.
    """
    return sorted(
        f.name
        for f in fields(cfg)
        if isinstance(getattr(cfg, f.name), str)
        and getattr(cfg, f.name).startswith("{")
        and "}" in getattr(cfg, f.name)
    )


def _deployed_selector_values():
    """LogQL selector values that actually deploy, read from the rendered env-secret.

    `_logql_selector_names()` reads the `Config` fields. This reads every key the render
    produces instead, so a selector written into a new key is checked even before any `Config`
    field reads it. It reads the RENDER rather than the template source because the selectors
    are Jinja over `loki_streams` (#3740), and the source text of one is not a selector.
    """
    return {
        key: value
        for key, value in bridge_env().items()
        if value.startswith("{") and "}" in value
    }


def test_the_selector_roster_covers_the_known_selectors(cfg):
    """A shape-derived roster that matches nothing passes every assertion vacuously, and one
    that matches fewer than the known selectors is a silent narrowing."""
    names = set(_logql_selector_names(cfg))
    known = {
        "LOKI_STREAM",
        "LOKI_DOCKER_STREAM",
        "LOKI_PI_STREAM",
        "HA_BAN_SELECTOR",
        "LOG_ERROR_SELECTOR",
    }
    assert known <= names, (
        "the derived roster no longer covers known selectors, so they are unchecked: %s"
        % sorted(known - names)
    )


def test_loki_selectors_use_real_stream_labels(cfg):
    for name in _logql_selector_names(cfg):
        selector = getattr(cfg, name)
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s selects on %s, which Alloy does not emit — the query matches no stream and "
            "the check goes permanently green: %s" % (name, sorted(unknown), selector)
        )


def test_the_deployed_selector_roster_covers_known_overrides():
    """A render that stopped producing the selectors would make `_deployed_selector_values()`
    return an empty dict, and every loop over it below would pass on nothing."""
    known = {
        "LOKI_STREAM",
        "LOKI_DOCKER_STREAM",
        "LOKI_PI_STREAM",
        "LOG_ERROR_SELECTOR",
    }
    deployed = set(_deployed_selector_values())
    assert known <= deployed, (
        "the deployed-selector roster no longer covers the known overrides, so they are "
        "unchecked: %s" % sorted(known - deployed)
    )


def test_deployed_loki_selectors_use_real_stream_labels():
    """Every rendered selector, including one no `Config` field reads yet, names real labels.

    This is a regression guard, not an active finding: the deployed selectors select on `job`
    and `machine`, which are real Alloy labels. It exists for the NEXT edit to a selector --
    HA_BAN_SELECTOR is the precedent (see this role's CLAUDE.md): an `app=` label matched no
    stream and read "no ip_ban events" permanently green.
    """
    for name, selector in _deployed_selector_values().items():
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s deploys as %s, which selects on %s -- Alloy does not emit that label, so "
            "the query matches no stream and the check goes permanently green"
            % (name, selector, sorted(unknown))
        )


# ── The cluster arms must not count the Pi ──────────────────────────────────────────────────
# Arms 1 and 2 of Loki Log Ingestion go down when the CLUSTER stops shipping. A daniel-pi stream
# they also match keeps their count above zero through a total cluster outage, so neither arm can
# fire (#3739). The label sets come from the `loki_streams` owner both Alloy configs render from.
_LOKI_STREAMS = load_yaml(ALL_VARS)["loki_streams"]
_PI_STREAMS = {
    role: {**labels, "container": "wg-easy"} if role == "pi_containers" else labels
    for role, labels in _LOKI_STREAMS.items()
    if labels.get("machine") == "daniel-pi"
}


def _selector_matches(selector, labels):
    """Whether a LogQL stream selector matches a stream carrying `labels`.

    Loki semantics: a matcher on an absent label compares against the empty string, so
    `machine!="daniel-pi"` matches a stream with no `machine` label, and a regex is anchored.
    """
    head = selector.split("}", 1)[0]
    for name, op, value in re.findall(r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"', head):
        got = labels.get(name, "")
        if op == "=":
            ok = got == value
        elif op == "!=":
            ok = got != value
        elif op == "=~":
            ok = re.fullmatch(value, got) is not None
        else:
            ok = re.fullmatch(value, got) is None
        if not ok:
            return False
    return True


def test_both_pi_streams_are_in_the_census():
    assert set(_PI_STREAMS) == {"pi_containers", "pi_health"}


@pytest.mark.parametrize(
    "key, cluster_role",
    [
        ("LOKI_STREAM", "host_syslog"),
        ("LOKI_STREAM", "host_authlog"),
        ("LOKI_DOCKER_STREAM", "cluster_pods"),
    ],
)
def test_cluster_arm_counts_its_cluster_stream(cfg, key, cluster_role):
    """The other half: a selector that excluded everything would also exclude the Pi."""
    labels = {
        **_LOKI_STREAMS[cluster_role],
        "machine": "daniel-box",
        "container": "authelia",
    }
    assert _selector_matches(getattr(cfg, key), labels)


@pytest.mark.parametrize("key", ["LOKI_STREAM", "LOKI_DOCKER_STREAM"])
def test_cluster_arm_counts_no_pi_stream(cfg, key):
    selector = getattr(cfg, key)
    held_open = sorted(
        r for r, labels in _PI_STREAMS.items() if _selector_matches(selector, labels)
    )
    assert not held_open, (
        "%s = %s also matches daniel-pi's %s stream, so the Pi keeps the arm's count above zero "
        "through a total cluster outage and it never fires" % (key, selector, held_open)
    )


@pytest.mark.parametrize(
    "key, selector",
    [
        # Both as deployed before #3739.
        ("LOKI_STREAM", '{job=~"authlog|syslog"}'),
        ("LOKI_DOCKER_STREAM", '{container=~".+"}'),
    ],
)
def test_cluster_arm_that_counts_a_pi_stream_is_flagged(key, selector):
    cfg = bridge.config.load_config(bridge_env(**{key: selector}))
    with pytest.raises(AssertionError, match="daniel-pi"):
        test_cluster_arm_counts_no_pi_stream(cfg, key)


def test_log_error_inert_when_the_selector_matches_nothing():
    """Zero total volume must report INERT, never OK.

    The arm fails open, so a wrong selector produces no matches and reads exactly like a
    healthy estate. This is the HA_BAN_SELECTOR trap generalised: a fail-open check goes green
    on a typo. The total-volume count is the only thing separating "nothing is wrong" from
    "I asked the wrong question", and this test is what keeps it load-bearing.
    """
    ok, msg = checks.logs.log_error_verdict([], 0, 20, "1h")
    assert ok
    assert "INERT" in msg, (
        "a selector matching no lines must SAY so, not read as healthy"
    )


def test_log_error_quiet_estate_is_ok():
    ok, msg = checks.logs.log_error_verdict([], 5000, 20, "1h")
    assert ok
    assert "no log-error bursts" in msg


def test_log_error_names_the_offending_container():
    ok, msg = checks.logs.log_error_verdict(
        [({"container": "grafana"}, 91.0), ({"container": "sonarr"}, 3.0)],
        5000,
        20,
        "1h",
    )
    assert not ok
    assert "grafana (91)" in msg
    assert "sonarr" not in msg, "a container under the threshold is not an offender"


def test_log_error_orders_offenders_worst_first():
    _, msg = checks.logs.log_error_verdict(
        [({"container": "quiet"}, 21.0), ({"container": "loud"}, 900.0)], 5000, 20, "1h"
    )
    assert msg.index("loud") < msg.index("quiet")


def test_log_error_ignore_list_is_case_insensitive():
    ok, _ = checks.logs.log_error_verdict(
        [({"container": "Chatty"}, 900.0)], 5000, 20, "1h", ignore={"chatty"}
    )
    assert ok


def test_log_error_burst_wins_the_message_over_healthy_workloads(monkeypatch, cfg):
    """A Ready-but-failing workload pages even though every Kubernetes arm reads healthy.

    That combination IS the finding: readiness asks whether the port is open.
    """
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}', LOG_ERROR_IGNORE="")
    monkeypatch.setattr(
        bridge.net,
        "log_error_counts",
        lambda _cfg, *a, **k: ([({"container": "grafana"}, 91.0)], 5000),
    )

    ok, msg = checks.logs.with_log_errors(cfg, True, "42 k8s workloads healthy")

    assert not ok
    assert msg.startswith("fatal log lines"), "the actionable arm leads"
    assert "42 k8s workloads healthy" in msg, (
        "the workload arm's text is kept, not dropped"
    )


def test_log_error_arm_fails_open_on_a_loki_outage(monkeypatch, cfg):
    """A Loki outage must not blind the three Kubernetes arms, which do not depend on it.

    This is why the check is NOT in LOKI_DEPENDENT: membership there suppresses the whole
    check, and Loki Reachable already owns that root cause.
    """
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}')

    def boom(_cfg, *a, **k):
        raise RuntimeError("loki query status=error")

    monkeypatch.setattr(bridge.net, "log_error_counts", boom)

    ok, msg = checks.logs.with_log_errors(cfg, False, "2 workloads unavailable")

    assert not ok, "the workload verdict survives the arm being unavailable"
    assert "2 workloads unavailable" in msg
    assert "log-error arm unavailable" in msg, "the arm must say it could not evaluate"


def _loki_scalar(val):
    """A Loki instant-query response for `sum(count_over_time(...))`. None -> empty result."""
    if val is None:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(val)]}],
        },
    }


def test_loki_ingestion_with_lines_is_ok():
    ok, msg = checks.logs.loki_ingestion_fresh(1234, "10m")
    assert ok
    assert "1234" in msg


def test_loki_ingestion_zero_lines_is_down():
    ok, msg = checks.logs.loki_ingestion_fresh(0, "10m")
    assert not ok
    assert "silent" in msg


def test_loki_ingestion_no_series_is_down():
    # an empty query result (no matching stream at all) is also a silent pipeline
    ok, _msg = checks.logs.loki_ingestion_fresh(None, "10m")
    assert not ok


def test_loki_count_parses_value(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "_get_json", lambda *a, **k: _loki_scalar(42))
    assert bridge.net.loki_count(cfg, '{job="syslog"}', "10m") == 42.0


def test_loki_count_empty_result_is_none(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "_get_json", lambda *a, **k: _loki_scalar(None))
    assert bridge.net.loki_count(cfg, '{job="syslog"}', "10m") is None


def test_loki_count_non_success_raises(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        bridge.net.loki_count(cfg, '{job="syslog"}', "10m")


def test_check_loki_ingestion_fresh_is_up(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "loki_count", lambda _cfg, *a, **k: 500)
    ok, _ = checks.logs.check_loki_ingestion(cfg)
    assert ok


def test_check_loki_ingestion_silent_is_down(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "loki_count", lambda _cfg, *a, **k: 0)
    ok, _msg = checks.logs.check_loki_ingestion(cfg)
    assert not ok


def test_check_loki_ingestion_docker_stream_silent_is_down(monkeypatch, cfg):
    # Pod-source failure: the file-tail streams keep flowing, but the highest-volume stream,
    # every pod's stdout, went silent. The file-tail arm alone stays non-zero and would hide
    # it — the pod-stream arm must page.
    def fake_count(_cfg, selector, window):
        return 0 if selector == cfg.LOKI_DOCKER_STREAM else 500

    monkeypatch.setattr(bridge.net, "loki_count", fake_count)
    ok, msg = checks.logs.check_loki_ingestion(cfg)
    assert not ok
    assert "container" in msg


def test_check_loki_ingestion_filetail_silent_is_down(monkeypatch, cfg):
    # File-tail-only failure: the pod streams keep flowing, but authlog/syslog went silent.
    # Arm 1's selector must EXCLUDE the pod streams so a healthy pod stream can't mask a dead
    # file-tail source — the file-tail arm must page.
    def fake_count(_cfg, selector, window):
        return 0 if selector == cfg.LOKI_STREAM else 500

    monkeypatch.setattr(bridge.net, "loki_count", fake_count)
    ok, msg = checks.logs.check_loki_ingestion(cfg)
    assert not ok
    assert "file-tail" in msg
