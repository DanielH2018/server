"""Fakes shared by the `alerts` tests: a Loki that honours the window, the limit and the cap."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from diagnostics.probe_lib import core


def _query_params(url):
    return parse_qs(urlparse(url).query)


def _fake_loki(lines):
    return {"data": {"result": [{"values": [[str(ts), line] for ts, line in lines]}]}}


# Loki's own rejection of a `limit` above `max_entries_limit_per_query`: HTTP 400 with this
# plaintext body, which `curl -sS` returns with exit 0. Copied from a live 2026-09-17 call.
LOKI_OVER_CAP_BODY = "max entries limit per query exceeded, limit > max_entries_limit_per_query ({limit} > {cap})"


def _route_alert_fetch(
    monkeypatch, per_query, respect_limit=False, cap=None, body=None
):
    """Serve each alert stream its own body, keyed by the LogQL in the url.

    `respect_limit` makes the fake behave the way Loki does under a cap: it keeps only the lines
    inside the requested window, then `limit` of them from the end `direction` names. Left off,
    every line is served whatever the url asked for — which is right for a test about filtering
    and wrong for a test about truncation, since a fake that ignores the limit passes whichever
    end the real query would have cut.

    `cap` makes the fake refuse a `limit` above it the way Loki does: the 400 body, as text.
    `body` serves that text verbatim to every call — any other error page a server can return.
    """
    import json as _json

    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        if body is not None:
            return body
        params = _query_params(url)
        if cap is not None and int(params["limit"][0]) > cap:
            return LOKI_OVER_CAP_BODY.format(limit=params["limit"][0], cap=cap)
        lines = per_query.get(params["query"][0], [])
        if respect_limit:
            start, end = int(params["start"][0]), int(params["end"][0])
            limit = int(params["limit"][0])
            window = [(ts, line) for ts, line in lines if start <= ts <= end]
            backward = params.get("direction", ["backward"])[0] == "backward"
            lines = window[-limit:] if backward else window[:limit]
        return _json.dumps(_fake_loki(lines))

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


def _two_day_log():
    """40 hourly DOWN lines ending an hour ago: `old_check` for a day, then `new_check`."""
    now_ns = int(datetime.now(UTC).timestamp() * 1e9)
    hour = int(3600 * 1e9)
    return sorted(
        [
            (now_ns - (i + 1) * hour, f"DOWN old_check - stale for {i}h")
            for i in range(20, 40)
        ]
        + [
            (now_ns - (i + 1) * hour, f"DOWN new_check - stale for {i}h")
            for i in range(20)
        ]
    )
