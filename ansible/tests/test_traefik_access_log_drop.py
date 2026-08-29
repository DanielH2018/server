"""The promtail drop rule for Traefik access logs, proved against real log lines.

The rule discards routine, fast Traefik access lines before they reach Loki — ~172 MB/day
of 200/204/304 measured 2026-08-29, dominated by homepage's own widget polling. It is a
REGEX over a JSON line, which is the fragile kind of rule: a field-order change upstream, or
an edit that widens one alternation, silently starts dropping the errors this is supposed to
keep, and the only symptom is logs that are not there.

A drop rule can fail in two directions and BOTH are silent, so both get a case here:

  * drops too little — the cost stays and nobody notices, since the logs still arrive;
  * drops too much   — 4xx/5xx or slow requests vanish, and the absence looks exactly like
    a quiet period.

So every assertion below is a pair: a line this MUST drop, and a line it MUST keep. A rule
that stopped matching entirely would pass a keep-only suite while saving nothing, and a rule
that matched everything would pass a drop-only suite while blinding the operator.

What this does NOT cover, deliberately: promtail evaluates the expression with Go's RE2 and
this file uses Python's `re`. The pattern is plain alternation plus a bounded digit class,
which both engines read identically. Anything reaching for a backreference or a lookaround
would need a live promtail to verify, and should not be written here in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_CONFIGMAP = _REPO / "ansible/roles/k8s/loki-homelab/templates/configmap.yaml.j2"

# The sidecar whose stdout carries the access log. CrowdSec tails the FILE instead, so this
# label is what keeps the drop away from the WAF's input — see the configmap's own comment.
_SCOPED_CONTAINER = "access-log-rotate"


def _drop_expression() -> str:
    """Pull the drop stage's expression out of the rendered promtail config.

    Read from the template text rather than a Jinja render: the k8s-pods job's pipeline has
    no conditionals in it, and parsing the real file is what makes this test notice an edit
    that moves or removes the stage rather than one that merely changes the pattern.
    """
    text = _CONFIGMAP.read_text()
    # The pipeline block is plain YAML inside the template — no Jinja between `pipeline_stages`
    # and `relabel_configs` on the k8s-pods job — so it can be sliced out and parsed.
    start = text.index("        pipeline_stages:\n")
    end = text.index("        relabel_configs:\n", start)
    block = yaml.safe_load(re.sub(r"^ {8}", "", text[start:end], flags=re.MULTILINE))

    matches = [stage["match"] for stage in block["pipeline_stages"] if "match" in stage]
    scoped = [m for m in matches if _SCOPED_CONTAINER in m["selector"]]
    assert scoped, (
        f"no promtail match stage is scoped to container={_SCOPED_CONTAINER!r}"
    )
    assert len(scoped) == 1, "more than one drop stage claims the access-log sidecar"

    drops = [s["drop"] for s in scoped[0]["stages"] if "drop" in s]
    assert len(drops) == 1, "the access-log match stage should hold exactly one drop"
    return drops[0]["expression"]


def _line(
    status: int, duration_ns: int, host: str = "homepage.local.example.com"
) -> str:
    """A Traefik JSON access line, fields in the order Traefik actually emits them.

    Traefik writes its access-log fields alphabetically, which is what puts DownstreamStatus
    ahead of Duration — the ordering the expression's `.*` depends on. Building the fixture
    in that same order is the point: a hand-written line with the fields reordered would let
    a broken pattern pass.
    """
    return (
        '{"ClientAddr":"10.42.1.92:46302","ClientHost":"10.42.1.92","ClientPort":"46302",'
        '"ClientUsername":"-","DownstreamContentSize":19,'
        f'"DownstreamStatus":{status},"Duration":{duration_ns},'
        '"GzipRatio":0,"OriginContentSize":0,"OriginDuration":0,"OriginStatus":0,'
        f'"Overhead":33011,"RequestAddr":"{host}","RequestContentSize":0,'
        f'"RequestCount":37950,"RequestHost":"{host}","RequestMethod":"GET"}}'
    )


def _drops(line: str) -> bool:
    return re.search(_drop_expression(), line) is not None


# ── routine traffic: must be dropped ────────────────────────────────────────────────────


def test_a_fast_200_is_dropped():
    """The bulk of the volume — 89 of 130 lines in the measured sample."""
    assert _drops(_line(200, 33_011))


def test_a_fast_304_is_dropped():
    """Conditional-GET hits from polling widgets: 38 of the same 130 lines."""
    assert _drops(_line(304, 1_200_000))


def test_a_fast_204_is_dropped():
    assert _drops(_line(204, 500_000))


# ── everything worth keeping: must survive ──────────────────────────────────────────────


def test_a_401_is_kept():
    """Auth failures are the signal an access log exists for."""
    assert not _drops(_line(401, 33_011))


def test_a_404_is_kept():
    assert not _drops(_line(404, 33_011))


def test_a_500_is_kept():
    assert not _drops(_line(500, 33_011))


def test_a_302_is_kept():
    """Authelia redirects. Cheap, and the first evidence of a redirect loop."""
    assert not _drops(_line(302, 2_769_115))


def test_a_slow_200_is_kept():
    """One second in nanoseconds is 10 digits, past the pattern's bound.

    A successful request that took this long is a latency symptom, and latency is exactly
    what a status-only rule would throw away.
    """
    assert not _drops(_line(200, 1_000_000_000))


def test_a_very_slow_304_is_kept():
    assert not _drops(_line(304, 8_400_000_000))


# ── the scope itself, which is what keeps CrowdSec's input intact ───────────────────────


def test_the_drop_is_scoped_to_the_access_log_sidecar_only():
    """An unscoped drop would apply to every pod's stdout in the cluster.

    The selector is also what keeps this away from CrowdSec: the agent reads the access-log
    FILE directly, and only the sidecar's stdout copy passes through this pipeline.
    """
    text = _CONFIGMAP.read_text()
    start = text.index("        pipeline_stages:\n")
    end = text.index("        relabel_configs:\n", start)
    block = yaml.safe_load(re.sub(r"^ {8}", "", text[start:end], flags=re.MULTILINE))

    for stage in block["pipeline_stages"]:
        assert "drop" not in stage, "a bare drop stage would apply to every k8s pod log"
