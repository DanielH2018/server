"""The Prometheus and Loki query client every caller shares: URL builders, window, transport.

Two callers that cannot share anything else share this. probe.py and postflight.py run on a
host and reach both stores through the VIP-pinned edge with curl (`core.fetch`,
`core.get_status`). homelab-mcp runs in a pod built from `python:3.14-slim`, which ships no
curl, and reaches both stores by their in-cluster Service names. Before this module each
built its own query strings and its own Loki window.

STDLIB ONLY, AND NO FIRST-PARTY IMPORT. homelab-mcp's image carries this file flat at
`/app/obs_api.py` (`ansible/roles/k8s/homelab-mcp/tasks/main.yml`), where neither `scripts/`
nor any `sys.path` bootstrap exists. A first-party import here breaks that pod at import time,
which no host-side test notices. `test_obs_api.py` imports this file with `scripts/` off
`sys.path` to catch exactly that.

An edit here maps to no deploy tag. The homelab-mcp image keeps the previous copy until its
next deploy, so deploy it with `./scripts/deploy.sh --tags "homelab-mcp"` after changing this.
"""

import json
import urllib.request
from urllib.parse import urlencode


def prom_query_url(base, promql):
    return f"{base}/api/v1/query?" + urlencode({"query": promql})


def prom_targets_url(base):
    return f"{base}/api/v1/targets"


def loki_labels_url(base):
    return f"{base}/loki/api/v1/labels"


def loki_query_url(base, logql, limit, start=None, end=None, direction=None):
    """Build a Loki `query_range` URL, omitting each optional param when it is unset.

    Args:
        base: The Loki base URL.
        logql: The LogQL query string.
        limit: Max lines to return.
        start: Range start, in nanoseconds since epoch.
        end: Range end, in nanoseconds since epoch.
        direction: `forward` or `backward`.
    """
    params = {"query": logql, "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    if direction is not None:
        params["direction"] = direction
    return f"{base}/loki/api/v1/query_range?" + urlencode(params)


def trailing_window_ns(seconds, now):
    """The (start_ns, end_ns) pair for the `seconds` before `now`, as Loki's query_range takes.

    Loki falls back to a ~1h lookback when start/end are omitted, so a query about anything
    older silently returns zero rows, which reads the same as a dead pipeline. Every Loki read
    therefore sends an explicit window built here.

    Raises:
        ValueError: `seconds` is not positive, which would ask for an empty or inverted window.
    """
    if seconds <= 0:
        raise ValueError("the window must be positive")
    return int((now - seconds) * 1e9), int(now * 1e9)


def get_json(url, timeout=15.0):
    """GET `url` and parse the body as JSON, for a caller that needs no `--resolve` pin.

    The in-cluster transport: a pod resolves Service names itself, so stdlib `urllib` is
    enough. A host caller goes through curl instead (`core.fetch_parsed`), because the edge
    route needs the VIP pin that `urllib` cannot express.

    Raises:
        urllib.error.HTTPError: the server answered 4xx/5xx.
        urllib.error.URLError: the request never got an answer.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)
