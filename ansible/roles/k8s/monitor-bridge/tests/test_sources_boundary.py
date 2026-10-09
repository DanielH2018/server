"""No check body reaches Prometheus, Loki or an HTTP API except through its `src` argument.

`bridge.sources.Sources` is the one seam a test answers (#3742). A check that called
`bridge.net.prom_vector(cfg, ...)` again would query the live transport behind a test that
believed it had stated the answer, and the suite would need its `bridge.net` patches back.
"""

import ast
import io
import json
import urllib.parse
from pathlib import Path

import pytest

from bridge.sources import Sink, Sources

FILES = Path(__file__).resolve().parents[1] / "files"

# The transport functions `Sources` wraps. `push` is the sink and the selector builders read
# no network, so neither is listed.
FETCHERS = frozenset(
    {
        "prom_scalar",
        "prom_vector",
        "loki_count",
        "loki_vector",
        "loki_lines",
        "_get_json",
        "_post_json",
    }
)


def _direct_fetches(source: str) -> list[str]:
    """Every `bridge.net.<fetcher>` attribute in `source`, called or passed as a value."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in FETCHERS
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "net"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "bridge"
        ):
            found.append("bridge.net.%s:%d" % (node.attr, node.lineno))
    return found


def _check_modules() -> dict[str, str]:
    paths = sorted((FILES / "checks").glob("*.py")) + [
        FILES / "check.py",
        FILES / "gates.py",
    ]
    return {p.relative_to(FILES).as_posix(): p.read_text() for p in paths}


def test_no_check_body_calls_the_transport_directly():
    modules = _check_modules()
    # Named members, so a move of the package reads as a failure rather than an empty pass.
    assert {"checks/host.py", "checks/cluster.py", "checks/logs.py", "check.py"} <= set(
        modules
    )
    offenders = {
        name: hits for name, src in modules.items() if (hits := _direct_fetches(src))
    }
    assert not offenders, (
        "query through the `src` argument (bridge.sources.Sources), not bridge.net: %s"
        % offenders
    )


def test_a_direct_fetch_is_flagged():
    assert _direct_fetches(
        "def check_x(cfg, src):\n    return bridge.net.prom_vector(cfg, 'up')\n"
    ) == ["bridge.net.prom_vector:2"]
    assert _direct_fetches("fetch = bridge.net._get_json\n") == [
        "bridge.net._get_json:1"
    ]


def test_a_query_through_src_is_clean():
    assert (
        _direct_fetches(
            "def check_x(cfg, src):\n"
            "    sel = bridge.net.origin_sel(cfg)\n"
            "    return src.prom_vector('up' + sel)\n"
        )
        == []
    )


# --- the live Sources reaches the endpoint each method names ---------------------------------

_VECTOR = {
    "status": "success",
    "data": {"result": [{"metric": {"job": "node"}, "value": [0, "2"]}]},
}
_STREAMS = {
    "status": "success",
    "data": {"result": [{"stream": {}, "values": [["5", "line"]]}]},
}


@pytest.mark.parametrize(
    ("call", "body", "path", "query", "expected"),
    [
        (lambda s: s.prom_scalar("up"), _VECTOR, "/api/v1/query", {"query": "up"}, 2.0),
        (
            lambda s: s.prom_vector("up"),
            _VECTOR,
            "/api/v1/query",
            {"query": "up"},
            [({"job": "node"}, 2.0)],
        ),
        (
            lambda s: s.loki_count('{job="x"}', "5m"),
            _VECTOR,
            "/loki/api/v1/query",
            {"query": 'sum(count_over_time({job="x"}[5m]))'},
            2.0,
        ),
        (
            lambda s: s.loki_vector("q"),
            _VECTOR,
            "/loki/api/v1/query",
            {"query": "q"},
            [({"job": "node"}, 2.0)],
        ),
        (
            lambda s: s.loki_lines("q", 60, 7),
            _STREAMS,
            "/loki/api/v1/query_range",
            {"query": "q", "limit": "7", "direction": "forward"},
            [(5, "line")],
        ),
        (
            lambda s: s.get_json("http://api.test/x?a=1"),
            {"ok": 1},
            "/x",
            {"a": "1"},
            {"ok": 1},
        ),
    ],
)
def test_the_live_sources_query_the_endpoint_each_method_names(
    cfg, call, body, path, query, expected
):
    """FakeSources overrides every method, so no other test runs these delegations.

    A swapped argument (selector for window, limit for window_s) type-checks and passes the
    whole suite while the pod queries nonsense.
    """
    sent, opener = _fake_opener(body)
    assert call(Sources(cfg, opener=opener)) == expected
    url = urllib.parse.urlsplit(sent[0].full_url)
    assert url.path.endswith(path)
    params = dict(urllib.parse.parse_qsl(url.query))
    assert query.items() <= params.items(), params


def test_the_live_post_json_sends_the_payload(cfg):
    sent, opener = _fake_opener({"ok": 1})
    assert Sources(cfg, opener=opener).post_json(
        "http://api.test/p", {"a": 1}, {"X-K": "v"}
    ) == {"ok": 1}
    assert sent[0].get_method() == "POST"
    assert json.loads(sent[0].data) == {"a": 1}
    assert sent[0].get_header("X-k") == "v"


def test_the_live_sink_pushes_to_the_token_s_monitor(cfg):
    """FakeSink overrides `push`, so this is the one test that runs the live delegation.

    A swapped token, status or message would pass every run-loop test and push nonsense to Kuma.
    """
    sent, opener = _fake_opener({"ok": True})
    Sink(cfg, opener=opener).push("tok123", False, "disk 95%")
    url = urllib.parse.urlsplit(sent[0].full_url)
    assert url.path.endswith("/api/push/tok123")
    assert dict(urllib.parse.parse_qsl(url.query)) == {
        "status": "down",
        "msg": "disk 95%",
    }


def _fake_opener(body):
    """An opener answering every request with `body` as JSON, and the list it records them in."""
    sent = []

    def opener(req, timeout=None):
        sent.append(req)
        return io.BytesIO(json.dumps(body).encode())

    return sent, opener
