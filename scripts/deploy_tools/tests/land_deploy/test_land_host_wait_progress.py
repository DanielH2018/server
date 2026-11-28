"""The progress the host wait writes into the landing's state record.

The helpers are `test_land_host_wait.py`'s, which drives the same wait over fake reads.

Run: uv run pytest scripts/deploy_tools/tests/land_deploy/test_land_host_wait_progress.py
"""

import json

from _land_fakes import make_landing
from deploy_tools.land_lib import host_wait
from deploy_tools.land_lib.host_wait import HostReads
from deploy_tools.land_lib.core.landing_state import State
from test_land_host_wait import MERGE, _at, _git, _pi, _prom, _series


def test_the_state_record_counts_the_hosts_applied_of_the_hosts_waited_for():
    stale, _ = _prom(_series(sha="c" * 12))
    fresh, _ = _prom(_series())
    answers = iter([stale] * 3 + [fresh] * 3)
    ln, calls = make_landing()
    ln.transitions.enter(State.HOST_WAIT)
    times = iter([_at(12, 0), _at(12, 1)])
    reads = HostReads(
        fetch=lambda url, timeout: next(answers)(url, timeout),
        git=_git(returncodes={"merge-base": 1}),
        clock=lambda: next(times),
        sleep=lambda seconds: None,
    )
    host_wait.wait_for_hosts(ln, [_pi()], MERGE, reads=reads)
    records = [json.loads(c[1][1]) for c in calls if c[0] == "record"]
    assert [(r["state"], r["progress"]) for r in records] == [
        ("host-wait", None),
        ("host-wait", {"done": 0, "total": 1, "what": "hosts applied"}),
        ("host-wait", {"done": 1, "total": 1, "what": "hosts applied"}),
    ]
