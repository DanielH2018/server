"""The Alloy drop rule for Traefik access logs, proved against real log lines.

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

What this does NOT cover, deliberately: Alloy evaluates the expression with Go's RE2 and
this file uses Python's `re`. The pattern is plain alternation plus a bounded digit class,
which both engines read identically. Anything reaching for a backreference or a lookaround
would need a live Alloy to verify, and should not be written here in the first place.
"""

import re

from _helpers import REPO

_REPO = REPO
_ALLOY_CONFIG = (
    _REPO / "ansible/roles/k8s/loki-homelab/templates/config/config.alloy.j2"
)

# The sidecar whose stdout carries the access log. CrowdSec tails the FILE instead, so this
# label is what keeps the drop away from the WAF's input — see the config's own comment.
_SCOPED_CONTAINER = "access-log-rotate"

# One `stage.match { selector = "…" stage.drop { … } }` block of the River config. The
# selector and the drop's attributes are River strings, so their inner quotes are `\"`.
_MATCH_BLOCK = re.compile(
    r'stage\.match\s*\{\s*selector\s*=\s*"(?P<selector>(?:\\.|[^"\\])*)"'
    r"(?P<body>.*?)\n  \}",
    re.DOTALL,
)
_DROP_EXPRESSION = re.compile(r'expression\s*=\s*"(?P<expr>(?:\\.|[^"\\])*)"')


def _drop_expression() -> str:
    """Pull the drop stage's expression out of the Alloy config.

    Read from the template text rather than a Jinja render: the pod pipeline has no
    conditionals in it, and parsing the real file is what makes this test notice an edit that
    moves or removes the stage rather than one that merely changes the pattern.
    """
    text = _ALLOY_CONFIG.read_text()
    matches = list(_MATCH_BLOCK.finditer(text))
    scoped = [m for m in matches if _SCOPED_CONTAINER in m["selector"]]
    assert scoped, f"no stage.match block is scoped to container={_SCOPED_CONTAINER!r}"
    assert len(scoped) == 1, "more than one drop stage claims the access-log sidecar"

    drops = _DROP_EXPRESSION.findall(scoped[0]["body"])
    assert len(drops) == 1, "the access-log match stage should hold exactly one drop"
    # River string escapes: `\"` is a literal quote in the regex Alloy compiles.
    return drops[0].replace('\\"', '"')


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
    text = _ALLOY_CONFIG.read_text()
    # Every stage.drop must sit inside a stage.match: a drop at the loki.process level
    # applies to every pod log the pipeline carries. Indentation is the nesting here — a
    # top-level stage is indented two spaces, one inside stage.match four.
    for line in text.splitlines():
        if line.lstrip().startswith("stage.drop"):
            assert line.startswith("    stage.drop"), (
                "a stage.drop outside stage.match would apply to every k8s pod log"
            )
