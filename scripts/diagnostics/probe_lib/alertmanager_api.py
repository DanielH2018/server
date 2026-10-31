"""Alertmanager's silences and inhibitions, which Prometheus' `ALERTS` series cannot show.

Two readers: `probe.py health <tag>` names the silence or inhibition on a gated alert, and
`probe.py monitors` lists the active silences. Both read through Alertmanager's GET-only LAN
door (`ansible/roles/k8s/observability/templates/alertmanager-read-ingressroute.yaml.j2`),
which admits `/api/v2/alerts` and `/api/v2/silences` alone. The design is answer 2 of
docs/target-architecture/monitoring/alertmanager-ui-and-readers.md.

Neither read decides a verdict. A silence mutes a page, not a deploy's health, so a silenced
alert still fails the gate; an unread Alertmanager prints a line saying so.
"""

from urllib.parse import urlencode

# `probe_lib` is a namespace package under `scripts/`, as in health_alerts.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch.
from diagnostics.probe_lib import core
from lib.exit_codes import PROBE_UNREADABLE


def endpoint():
    """(base_url, curl --resolve pin) for the read door."""
    return core.k8s_endpoint("alertmanager")


def alerts_url(base, tag):
    """Every alert Alertmanager holds for `tag`, silenced and inhibited ones included."""
    return f"{base}/api/v2/alerts?" + urlencode({"filter": f'service="{tag}"'})


def silences_url(base):
    return f"{base}/api/v2/silences"


def read(url, pin, fetch=None):
    """The JSON list Alertmanager answers at `url`.

    Raises:
        core.Unreadable: curl failed, or the body is not a JSON list (a Traefik 404 page when
            the door is not deployed).
    """
    data = (fetch or core.fetch_parsed)(url, resolve=pin)
    if not isinstance(data, list):
        raise core.Unreadable(f"Alertmanager answered {url} with {str(data)[:300]}")
    return data


def service_alerts(tag):
    """Alertmanager's alerts for `tag`, read through the door."""
    base, pin = endpoint()
    return read(alerts_url(base, tag), pin)


def is_gated(labels):
    """Whether an alert with `labels` is in C7's gated set, as `gated_selectors` selects it."""
    return (
        labels.get("shadow") != "true"
        and labels.get("alertname") != "Watchdog"
        and (labels.get("severity") == "critical" or labels.get("verify") == "true")
    )


def suppression_lines(alerts):
    """One line per gated alert in `alerts` that a silence or an inhibition mutes. Pure."""
    lines = []
    for alert in alerts:
        labels = alert.get("labels") or {}
        status = alert.get("status") or {}
        if not is_gated(labels):
            continue
        name = (
            f"{labels.get('alertname', '?')}{{severity={labels.get('severity', '?')}}}"
        )
        for field, how in (
            ("silencedBy", "silenced by"),
            ("inhibitedBy", "inhibited by"),
        ):
            if status.get(field):
                lines.append(
                    f"  alertmanager: {name} {how} {', '.join(status[field])}; "
                    "a muted page still fails this gate"
                )
    return sorted(lines)


def gate_notes(tag, alerts=service_alerts):
    """`suppression_lines` for `tag`, or one note saying Alertmanager went unread.

    `alerts` is the read, which a test replaces.
    """
    try:
        return suppression_lines(alerts(tag))
    except core.Unreadable as exc:
        return [
            f"  note: Alertmanager unread, so silences and inhibitions are unknown: {exc}"
        ]


# (isEqual, isRegex) to the matcher operator, as Alertmanager's own UI prints it.
_OPS = {
    (True, False): "=",
    (True, True): "=~",
    (False, False): "!=",
    (False, True): "!~",
}


def _matcher(m):
    op = _OPS[(bool(m.get("isEqual", True)), bool(m.get("isRegex")))]
    return f'{m.get("name", "?")}{op}"{m.get("value", "")}"'


def format_silences(silences):
    """(lines, exit code) for the active silences in `silences`, one line each. Pure.

    `silences` is the parsed `/api/v2/silences` list, or a string saying why it went unread.
    Expired and pending silences are left out. A silence is not a problem, so the code is 0
    unless the read failed, which is PROBE_UNREADABLE.
    """
    if isinstance(silences, str):
        return [f"  alertmanager silences unread: {silences}"], PROBE_UNREADABLE
    lines = []
    for s in silences:
        if (s.get("status") or {}).get("state") != "active":
            continue
        matchers = ",".join(_matcher(m) for m in s.get("matchers") or [])
        lines.append(
            f"  {s.get('id', '?')} {{{matchers}}} until {s.get('endsAt', '?')} "
            f"by {s.get('createdBy', '?')}: {s.get('comment', '')}"
        )
    return sorted(lines), 0


def read_silences():
    """Every silence Alertmanager holds, read through the door, expired ones included."""
    base, pin = endpoint()
    return read(silences_url(base), pin)


def report(code, silences=read_silences):
    """Print the active silences, and fold an unread Alertmanager into `monitors`' exit `code`.

    A problem elsewhere (1) wins; otherwise an unread Alertmanager exits PROBE_UNREADABLE, so
    two clean reads are the only exit 0. `silences` is the read, which a test replaces.
    """
    try:
        data = silences()
    except core.Unreadable as exc:
        data = exc.message
    lines, am_code = format_silences(data)
    if lines:
        out = _sys.stderr if am_code == PROBE_UNREADABLE else _sys.stdout
        print("\n".join(["alertmanager silences:", *lines]), file=out)
    return code if code == 1 else max(code, am_code)
