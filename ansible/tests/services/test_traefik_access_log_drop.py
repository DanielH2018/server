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

The config is read out of the RENDERED ConfigMap, not out of `templates/config/config.alloy.j2`
(#3107). Reading the template proved the rule was written; it could not prove the rule ships.
`configmap.yaml.j2` pulls the file in through one `lookup('template', …)` line, and deleting
that line left every assertion here green while the Alloy pod ran with no drop stage at all.
The file is not conditional-free either — the k8s audit-log stages sit behind
`k3s_audit_log_enabled` — so a text scan reads stages the pod may never load.

Rendered, the River text arrives indented as the pod sees it: the ConfigMap embeds it with
`indent(4, true)`, and parsing the manifest as YAML strips exactly that block-scalar indent.
So the stage regexes and the two-space nesting rule below read the rendered config unchanged,
which is why this conversion is a swap of the source rather than a rewrite of the assertions.

What this does NOT cover, deliberately: Alloy evaluates the expression with Go's RE2 and
this file uses Python's `re`. The pattern is plain alternation plus a bounded digit class,
which both engines read identically. Anything reaching for a backreference or a lookaround
would need a live Alloy to verify, and should not be written here in the first place.
"""

import re

import pytest

from _k8s_render import rendered_docs

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


@pytest.fixture(scope="module")
def alloy_config() -> str:
    """The Alloy config as the DaemonSet mounts it, out of loki-homelab's ConfigMap.

    The non-vacuity assertion is the point of the fixture: every rule below pulls a stage out
    of this string, and an empty one would make each of them fail naming the stage rather than
    naming the ConfigMap key that went missing.
    """
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            config = (doc.get("data") or {}).get("config.alloy")
            assert config, (
                "loki-homelab's ConfigMap carries no config.alloy — the Alloy pod mounts "
                "this key, so nothing below is the pipeline that runs"
            )
            return config
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


def _drop_expression(config: str) -> str:
    """Pull the drop stage's expression out of the rendered Alloy config."""
    matches = list(_MATCH_BLOCK.finditer(config))
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


def _drops(config: str, line: str) -> bool:
    return re.search(_drop_expression(config), line) is not None


# ── routine traffic: must be dropped ────────────────────────────────────────────────────


def test_a_fast_200_is_dropped(alloy_config):
    """The bulk of the volume — 89 of 130 lines in the measured sample."""
    assert _drops(alloy_config, _line(200, 33_011))


def test_a_fast_304_is_dropped(alloy_config):
    """Conditional-GET hits from polling widgets: 38 of the same 130 lines."""
    assert _drops(alloy_config, _line(304, 1_200_000))


def test_a_fast_204_is_dropped(alloy_config):
    assert _drops(alloy_config, _line(204, 500_000))


# ── everything worth keeping: must survive ──────────────────────────────────────────────


def test_a_401_is_kept(alloy_config):
    """Auth failures are the signal an access log exists for."""
    assert not _drops(alloy_config, _line(401, 33_011))


def test_a_404_is_kept(alloy_config):
    assert not _drops(alloy_config, _line(404, 33_011))


def test_a_500_is_kept(alloy_config):
    assert not _drops(alloy_config, _line(500, 33_011))


def test_a_302_is_kept(alloy_config):
    """Authelia redirects. Cheap, and the first evidence of a redirect loop."""
    assert not _drops(alloy_config, _line(302, 2_769_115))


def test_a_slow_200_is_kept(alloy_config):
    """One second in nanoseconds is 10 digits, past the pattern's bound.

    A successful request that took this long is a latency symptom, and latency is exactly
    what a status-only rule would throw away.
    """
    assert not _drops(alloy_config, _line(200, 1_000_000_000))


def test_a_very_slow_304_is_kept(alloy_config):
    assert not _drops(alloy_config, _line(304, 8_400_000_000))


# ── the scope itself, which is what keeps CrowdSec's input intact ───────────────────────


def test_the_drop_is_scoped_to_the_access_log_sidecar_only(alloy_config):
    """An unscoped drop would apply to every pod's stdout in the cluster.

    The selector is also what keeps this away from CrowdSec: the agent reads the access-log
    FILE directly, and only the sidecar's stdout copy passes through this pipeline.
    """
    # Every stage.drop must sit inside a stage.match: a drop at the loki.process level
    # applies to every pod log the pipeline carries. Indentation is the nesting here — a
    # top-level stage is indented two spaces, one inside stage.match four.
    for line in alloy_config.splitlines():
        if line.lstrip().startswith("stage.drop"):
            assert line.startswith("    stage.drop"), (
                "a stage.drop outside stage.match would apply to every k8s pod log"
            )
