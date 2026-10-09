"""No check body reaches Prometheus, Loki or an HTTP API except through its `src` argument.

`bridge.sources.Sources` is the one seam a test answers (#3742). A check that called
`bridge.net.prom_vector(cfg, ...)` again would query the live transport behind a test that
believed it had stated the answer, and the suite would need its `bridge.net` patches back.
"""

import ast
from pathlib import Path

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
