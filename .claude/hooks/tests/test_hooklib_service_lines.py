#!/usr/bin/env python3
"""The banner's scrape-target section.

These drive `hooklib.service_lines` through its `run` and `scaled_to_zero` seams rather than
patching a module attribute, so they need no live cluster or Prometheus.

Run: uv run pytest .claude/hooks
"""

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hooklib import service_lines


def _result(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _raises(exc):
    def boom(*_a, **_k):
        raise exc

    return boom


_TARGETS_ONE_DOWN = (
    '{"data":{"activeTargets":['
    '{"health":"up","labels":{"job":"traefik","instance":"traefik:8080"}},'
    '{"health":"down","labels":{"job":"loki","instance":"loki:3100"},'
    '"lastError":"connection refused"}'
    "]}}"
)

_TARGETS_ONE_SCALED_TO_ZERO = (
    '{"data":{"activeTargets":['
    '{"health":"down","labels":{"job":"terraria-stats","instance":"terraria-stats:9420"},'
    '"lastError":"connection refused"},'
    '{"health":"down","labels":{"job":"loki","instance":"loki:3100"},'
    '"lastError":"connection refused"}'
    "]}}"
)


def _targets(payload, scaled_to_zero=lambda run, job, ns: False):
    return service_lines.target_problems(
        lambda *a, **k: _result(payload),
        "/repo",
        namespace="homelab",
        scaled_to_zero=scaled_to_zero,
    )


def test_target_problems_asks_for_the_raw_json():
    """Plain `probe.py targets` prints a formatted view, which json.loads cannot read (#7896)."""
    argvs = []

    def run(argv, _timeout):
        argvs.append(argv)
        return _result(_TARGETS_ONE_DOWN)

    service_lines.target_problems(
        run, "/repo", namespace="homelab", scaled_to_zero=lambda *a: False
    )
    assert argvs[0][-2:] == ["targets", "--json"]


def test_target_problems_flags_down():
    bad = _targets(_TARGETS_ONE_DOWN)
    assert len(bad) == 1
    assert "loki" in bad[0] and "connection refused" in bad[0]


def test_target_problems_all_up():
    assert (
        _targets('{"data":{"activeTargets":[{"health":"up","labels":{"job":"x"}}]}}')
        == []
    )


def test_target_problems_swallows_bad_json():
    # A monitoring hiccup must never blow up the hook.
    assert _targets("not json") == []


def test_target_problems_filters_scaled_to_zero_deployments():
    # terraria-stats/valheim-stats are on-demand game servers deliberately scaled to 0 —
    # reporting them every session open forever is exactly the noise the all-green contract
    # exists to avoid, so only the genuinely-unexplained loki target survives.
    bad = _targets(
        _TARGETS_ONE_SCALED_TO_ZERO,
        scaled_to_zero=lambda run, job, ns: job == "terraria-stats",
    )
    assert len(bad) == 1
    assert "loki" in bad[0]
    assert not any("terraria-stats" in line for line in bad)


def test_is_scaled_to_zero_true_when_replicas_zero():
    assert (
        service_lines.is_scaled_to_zero(
            lambda *a, **k: _result("0"), "terraria-stats", "homelab"
        )
        is True
    )


def test_is_scaled_to_zero_false_when_replicas_nonzero():
    assert (
        service_lines.is_scaled_to_zero(lambda *a, **k: _result("1"), "loki", "homelab")
        is False
    )


def test_is_scaled_to_zero_false_on_kubectl_failure():
    # An unreadable answer must not be treated as "confirmed intentional" — a real problem
    # we can't explain has to stay visible, the same fail-open-to-visible rule probe.py's
    # format_k8s_health uses for an unreadable restart time.
    assert (
        service_lines.is_scaled_to_zero(
            lambda *a, **k: _result("", returncode=1), "sonarr", "homelab"
        )
        is False
    )


def test_is_scaled_to_zero_false_on_timeout():
    assert (
        service_lines.is_scaled_to_zero(
            _raises(subprocess.TimeoutExpired("kubectl", 5)), "sonarr", "homelab"
        )
        is False
    )


def test_is_scaled_to_zero_false_without_a_namespace():
    assert (
        service_lines.is_scaled_to_zero(_raises(AssertionError), "sonarr", None)
        is False
    )


def test_k8s_namespace_reads_the_inventory_file(tmp_path):
    group_vars = tmp_path / "inventory" / "group_vars"
    group_vars.mkdir(parents=True)
    (group_vars / "all.yml").write_text("other: x\nk8s_namespace: homelab\n")
    assert service_lines.k8s_namespace(str(tmp_path)) == "homelab"


def test_k8s_namespace_is_none_when_the_file_is_missing(tmp_path):
    assert service_lines.k8s_namespace(str(tmp_path)) is None
