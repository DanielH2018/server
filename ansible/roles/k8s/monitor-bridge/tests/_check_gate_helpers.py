"""The `run_once` drivers the gate suites share.

Four test modules drive one full check cycle with the transport stubbed and one gate forced
into a state — `test_check_gates.py`, `test_check_gates_exporters.py`, `test_check_b2_gate.py`
and `test_check_streaks.py`. Each used to carry its own near-identical `_wire_run_once`, and
each patched `check.CHECKS` and the four gate bodies onto the `check` module to do it.

Since the `Gates` seam they state the gate configuration instead, which is short enough to
share: one `Gates(...)` and one `checks` list per driver. A module with a leading underscore
rather than a `conftest.py` fixture, because these take arguments and return values — a fixture
would have to be a factory returning a function, which reads worse than the import. The name is
unique repo-wide, which is what `from conftest import ...` cannot promise.
"""

import bridge.common
import bridge.net
import check
from bridge.types import Check
from gates import Gates


def mk(ran: list[str], name: str):
    """A check body that records that it ran, then reports `ok`."""

    def fn(_cfg):
        ran.append(name)
        return True, "%s ok" % name

    return fn


def as_probe(result):
    """A gate probe body returning `result`, or raising it when it is an Exception.

    An unreachable dependency raises out of its probe rather than returning `(False, msg)`, and
    `_evaluate` is what turns that into a down verdict — so a driver that could only return a
    pair would never exercise the real outage path.
    """
    if isinstance(result, Exception):

        def _probe(_cfg):
            raise result
    else:

        def _probe(_cfg):
            return result

    return _probe


def wire_run_once(cfg, monkeypatch, prom_result):
    """Drive run_once with a tiny registry (one prom-dependent, one not) and capture pushes.

    Returns (ran, pushes): `ran` is the names of checks actually executed, `pushes` is
    [(token, ok, msg), ...] in push order (incl. the leading `prometheus` push).
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, token, ok, msg: pushes.append((token, ok, msg))
    )
    # No exporters down by default, so the prom-up path doesn't hit the network probing `up`.
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, q: [])
    checks = [
        Check("disk", "tok_disk", mk(ran, "disk")),
        Check("backup", "tok_backup", mk(ran, "backup")),
    ]
    check.run_once(
        cfg,
        checks,
        gates=Gates(
            prom_dependent=frozenset({"disk"}),
            probe_prometheus=as_probe(prom_result),
            # Loki reachable by default so run_once's Loki gate makes no real network call here.
            probe_loki=lambda _cfg: (True, "loki ok"),
            probe_wan=lambda _cfg: (True, "wan ok"),
        ),
    )
    return ran, pushes


def wire_run_once_reachability(
    cfg,
    monkeypatch,
    names,
    loki_result=(True, "loki ok"),
    loki_dependent=(),
    wan_result=(True, "wan ok"),
    wan_dependent=(),
):
    """Drive run_once with Prometheus UP and a stated Loki and WAN result; capture run+push.

    The two gates share one driver because they are the same shape — a reach-out probe whose
    verdict suppresses a named set — and a second near-identical driver would spend two more
    monkeypatch sites saying nothing new. Each gate defaults to reachable with an empty
    dependent set, so a caller states only the gate it is about.

    Args:
      names: The check names to register, each a body that records that it ran.
      loki_result: What `probe_loki` reports, or an Exception for it to raise.
      loki_dependent: The names the Loki gate suppresses.
      wan_result: What `probe_wan` reports, or an Exception for it to raise.
      wan_dependent: The names the WAN gate suppresses.

    Returns:
      (ran, pushes) — the names that actually executed, and [(token, ok, msg), ...].
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, t, ok, m: pushes.append((t, ok, m))
    )
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, q: [])
    check.run_once(
        cfg,
        [Check(n, "tok_%s" % n, mk(ran, n)) for n in names],
        gates=Gates(
            prom_dependent=frozenset(),
            loki_dependent=frozenset(loki_dependent),
            wan_dependent=frozenset(wan_dependent),
            probe_prometheus=lambda _cfg: (True, "prom ok"),
            probe_loki=as_probe(loki_result),
            probe_wan=as_probe(wan_result),
        ),
    )
    return ran, pushes
