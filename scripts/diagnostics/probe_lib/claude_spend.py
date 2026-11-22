"""`probe.py claude-spend` -- Claude Code spend per session kind, desktop coordinators split out.

A launcher this repo owns labels its sessions with `session_kind`
(`scripts/dev/fanout_lib/telemetry.py:SessionKind`). The desktop app starts a session with no
environment, so a desktop session carries no such label, and the root coordinator, every
sub-coordinator and the operator's own desktop sessions share one unlabelled bucket.

The cost counter carries `session_id`, and so does every Loki event. A desktop session counts
as a coordinator when, inside the window, it called the `coordinate` skill or the app's
`start_session` tool. Every other unlabelled desktop session counts as interactive.

The join undercounts coordinators in two cases. A coordinator that called neither tool inside
the window reads as interactive. A resumed session gets a new `session_id`, so its spend after
the resume reads as interactive until it calls one of the two tools again.

A report, not a verdict: it exits 0 once both stores answered, and 75 when one did not.
"""

import time
from dataclasses import dataclass

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health_alerts import prom_query
from diagnostics.probe_lib.obs_api import loki_query_url

DESKTOP = "claude-desktop"
COORDINATOR = "desktop-coordinator"
INTERACTIVE = "desktop-interactive"
COORDINATOR_TOOLS = ("coordinate", "start_session")


def cost_query(window_s: int) -> str:
    """Each session's Claude Code spend over the last `window_s` seconds."""
    return (
        "sum by (app_entrypoint, session_kind, session_id) "
        f"(increase(claude_code_cost_usage_USD_total[{window_s}s])) > 0"
    )


def coordinator_logql(window_s: int) -> str:
    """The desktop sessions that called a coordinator tool in the last `window_s` seconds.

    The `coordinate` skill reaches Loki as `{"skill_name":"coordinate"}` and the app's tool as
    `{"mcp_server_name":"ccd_session","mcp_tool_name":"start_session"}`.
    """
    names = "|".join(COORDINATOR_TOOLS)
    return (
        'sum by (session_id) (count_over_time({service_name="claude-code"} '
        f'| app_entrypoint="{DESKTOP}" | event_name="tool_result" '
        f'| tool_parameters=~`.*"({names})".*` [{window_s}s]))'
    )


@dataclass(frozen=True)
class SessionCost:
    """One session's spend over the window, as Prometheus labels it."""

    session_id: str
    entrypoint: str
    labelled_kind: str
    usd: float

    @property
    def kind(self) -> str:
        """The session's kind, before the coordinator join reaches a desktop session."""
        return self.labelled_kind or f"unlabelled {self.entrypoint or '?'}"


def parse_costs(result: list) -> list[SessionCost]:
    """An instant vector of `cost_query` as one `SessionCost` per series."""
    return [
        SessionCost(
            session_id=s["metric"].get("session_id", ""),
            entrypoint=s["metric"].get("app_entrypoint", ""),
            labelled_kind=s["metric"].get("session_kind", ""),
            usd=float(s["value"][1]),
        )
        for s in result
    ]


def parse_session_ids(data: dict) -> frozenset[str]:
    """The `session_id` of every series in a Loki metric response."""
    return frozenset(
        s["metric"]["session_id"]
        for s in (data.get("data") or {}).get("result") or []
        if s.get("metric", {}).get("session_id")
    )


def session_kind(cost: SessionCost, coordinators: frozenset[str]) -> str:
    """The kind a session's spend is reported under.

    A labelled session keeps its label. An unlabelled desktop session is a coordinator when
    `coordinators` holds its id and interactive otherwise.
    """
    if cost.labelled_kind or cost.entrypoint != DESKTOP:
        return cost.kind
    return COORDINATOR if cost.session_id in coordinators else INTERACTIVE


def format_spend(
    costs: list[SessionCost], coordinators: frozenset[str], window: str
) -> list[str]:
    """The report: a total line, then one row per kind, the costliest first."""
    totals: dict[str, float] = {}
    sessions: dict[str, int] = {}
    for cost in costs:
        kind = session_kind(cost, coordinators)
        totals[kind] = totals.get(kind, 0.0) + cost.usd
        sessions[kind] = sessions.get(kind, 0) + 1
    total = sum(totals.values())
    lines = [f"${total:,.2f} over {window} across {len(costs)} sessions"]
    if not costs:
        return lines
    lines.append(f"{'KIND':<26} {'USD':>10} {'SHARE':>6} {'SESSIONS':>8}")
    for kind in sorted(totals, key=lambda k: -totals[k]):
        lines.append(
            f"{kind:<26} {totals[kind]:>10,.2f} {totals[kind] / total:>6.0%} "
            f"{sessions[kind]:>8}"
        )
    return lines


def run_claude_spend(ns) -> int:
    """Print the spend report over `--since`; 0 once both stores answered.

    Raises:
        core.Unreadable: Prometheus or Loki did not answer.
    """
    window_s = core.parse_duration_seconds(ns.since)
    costs = parse_costs(prom_query(cost_query(window_s)))
    base, pin = core.loki_endpoint("observability")
    now_ns = int(time.time() * 1e9)
    url = loki_query_url(
        base, coordinator_logql(window_s), 5000, start=now_ns, end=now_ns, step=60
    )
    data = core.fetch_parsed(url, resolve=pin)
    if not isinstance(data, dict) or data.get("status") != "success":
        raise core.Unreadable(f"Loki refused the coordinator query: {str(data)[:300]}")
    print("\n".join(format_spend(costs, parse_session_ids(data), ns.since)))
    return 0
