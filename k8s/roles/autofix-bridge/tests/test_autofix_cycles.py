"""autofix-bridge's grace, cap and hold alert, over generated sequences of cycles.

The machine draws a grace and a cap, then runs `run_once` live (`DRY_RUN=false`) against a fake
Sonarr queue whose candidates it draws each cycle, and a Discord webhook whose posts it makes
succeed or fail. It keeps its own count of each key's consecutive cycles as a candidate and
checks every cycle's DELETEs and hold alert against that count. The fake replaces
`urllib.request.urlopen`, so the cycle reaches the queue and the webhook through the module's
own `_request` and `post_discord`. The seed and the example count come from
`tests/hypothesis_profiles.py`.

Run: uv run pytest k8s/roles/autofix-bridge/tests/test_autofix_cycles.py
"""

import importlib.util
import json
import pathlib
import urllib.error
import urllib.request

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    precondition,
    rule,
    run_state_machine_as_test,
)

_SPEC = importlib.util.spec_from_file_location(
    "autofix_cycles_under_test",
    pathlib.Path(__file__).resolve().parent.parent / "files" / "autofix.py",
)
assert _SPEC and _SPEC.loader, "spec_from_file_location found no loader"

KEYS = tuple("hash%d" % i for i in range(8))
WEBHOOK = "https://discord.example/webhook"


class _Response:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _queue_item(key, candidate):
    item = {"downloadId": key, "id": KEYS.index(key), "title": key, "seriesId": 1}
    if candidate:
        item["trackedDownloadStatus"] = "error"
    else:
        item["trackedDownloadState"] = "downloading"
    return item


class Cycles(RuleBasedStateMachine):
    """One bridge process, cycled by Hypothesis, with the model of its streaks and alerts."""

    def __init__(self):
        super().__init__()
        self.patch = pytest.MonkeyPatch()
        self.patch.setattr(urllib.request, "urlopen", self.urlopen)
        self.candidates = frozenset()
        self.deleted = []
        self.posts = []
        self.post_ok = True
        self.streaks = {}
        # The held set the last delivered alert named; None once a post fails or a cycle holds
        # nothing, so the next hold must alert.
        self.alerted = None
        self.failed_hold = None
        self.last = None

    def teardown(self):
        self.patch.undo()

    def record(self, kind: str) -> None:
        """A hook for the coverage test."""

    def urlopen(self, req, timeout=None):
        if req.full_url == WEBHOOK:
            self.posts.append(json.loads(req.data)["content"])
            if not self.post_ok:
                raise urllib.error.URLError("webhook refused the post")
            return _Response()
        if req.get_method() == "GET":
            records = [_queue_item(k, k in self.candidates) for k in KEYS]
            return _Response(json.dumps({"records": records}).encode())
        if req.get_method() == "DELETE":
            self.deleted.append(KEYS[int(req.full_url.split("?")[0].rsplit("/", 1)[1])])
        return _Response()

    @initialize(grace=st.integers(1, 4), cap=st.integers(0, 5))
    def start(self, grace, cap):
        self.grace, self.cap = grace, cap
        for name, value in {
            "SONARR_API_KEY": "sonarr-key",
            "RADARR_API_KEY": "",
            "ARR_DISCORD_WEBHOOK_URL": WEBHOOK,
            "DRY_RUN": "false",
            "GRACE_CYCLES": str(grace),
            "MAX_ACTIONS_PER_CYCLE": str(cap),
        }.items():
            self.patch.setenv(name, value)
        assert _SPEC and _SPEC.loader
        self.autofix = importlib.util.module_from_spec(_SPEC)
        _SPEC.loader.exec_module(self.autofix)
        self.state = self.autofix.CycleState()

    @rule(ok=st.booleans())
    def post_result(self, ok):
        self.post_ok = ok

    @rule(candidates=st.sets(st.sampled_from(KEYS)))
    def cycle(self, candidates):
        self.run_cycle(frozenset(candidates))

    @precondition(lambda self: self.last is not None)
    @rule()
    def same_queue(self):
        self.run_cycle(self.last)

    def run_cycle(self, candidates):
        self.last = self.candidates = candidates
        self.streaks = {k: self.streaks.get(k, 0) + 1 for k in candidates}
        met = frozenset(k for k, n in self.streaks.items() if n >= self.grace)
        held = met if len(met) > self.cap else frozenset()
        self.deleted, self.posts = [], []

        ok, msg = self.autofix.run_once(self.state)

        acted = set(self.deleted)
        assert acted <= candidates
        assert all(self.streaks[k] >= self.grace for k in acted)
        assert len(acted) <= self.cap
        holding = msg.startswith("Holding")
        assert not (acted and holding)
        assert holding == bool(held) == (not ok)
        assert acted == (set() if held else met)

        alerts = [p for p in self.posts if p.startswith("Holding")]
        if not held:
            assert alerts == []
            self.alerted = None
            if acted:
                self.record("act")
            return
        assert msg == self.autofix.format_hold(
            [("Sonarr", k) for k in sorted(held)], self.cap
        )
        if held == self.alerted:
            assert alerts == [], "alerted twice for one held set"
            self.record("quiet")
            return
        assert alerts == [msg]
        self.record("retry" if held == self.failed_hold else "alert")
        self.alerted = held if self.post_ok else None
        self.failed_hold = None if self.post_ok else held


def test_every_cycle_respects_grace_cap_and_hold_over_draws_reaching_every_outcome():
    """The machine's checks hold on every cycle, over draws that reach every outcome.

    The outcome check is the non-vacuity half: a draw that never held, or never retried a failed
    alert, passes every per-cycle check.
    """
    seen = set()

    class Recording(Cycles):
        def record(self, kind: str) -> None:
            seen.add(kind)

    run_state_machine_as_test(Recording)
    assert seen == {"act", "alert", "quiet", "retry"}
