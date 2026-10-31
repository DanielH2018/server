"""`FakeSources` and `FakeSink`: the `bridge.sources` doubles a test hands to a check or `run_once`.

A test states the answer each query gets instead of patching `bridge.net`:

    src = FakeSources(prom_vector=lambda q: [({"origin": "daniel-box"}, 50.0)])
    ok, msg = checks.gitops.check_host_packages(cfg, src)

Each keyword is one `Sources` method, given as a callable taking that method's arguments
WITHOUT `cfg` (the live object binds it). A method the test did not answer raises
`AssertionError`, so a check that queries something the test did not expect fails loudly
rather than reading an empty answer as a healthy estate. `FakeSources()` with no answers is
therefore the fake for a check that must do no I/O at all.

`calls` records every query in order as `(method, args)`, for a test that asserts on the
PromQL or URL a check sent. `state` is a fresh `bridge.streaks.State`, so a test seeds or reads a
streak or probe cache as `src.state.<field>` rather than through a module global. A test whose
cycles need different answers builds a second fake with `FakeSources(state=first.state, ...)`.

`FakeSink` records every push `run_once` would have sent to Kuma as `(token, ok, msg)` in
`pushes`, so a test reads what was pushed without patching `bridge.net.push`.

`mk` and `as_body` build the check bodies a registry of `Check` entries carries: one that records
that it ran, and one that returns or raises a stated result. The second exists because an
unreachable dependency raises out of its probe rather than returning `(False, msg)`, and
`_evaluate` is what turns that into a down verdict, so a driver that could only return a pair
would never exercise the real outage path.

A module with a leading underscore rather than a `conftest.py` fixture: these take arguments and
return values, which a fixture would have to wrap in a factory, and the name is unique repo-wide.
"""

from collections.abc import Callable

from bridge.sources import Sink, Sources
from bridge.streaks import State

_METHODS = (
    "prom_scalar",
    "prom_vector",
    "loki_count",
    "loki_lines",
    "get_json",
    "get_bytes",
    "post_json",
)


class FakeSources(Sources):
    """A `Sources` whose every query is answered by a callable the test supplies.

    Attributes:
      calls: `(method, args)` for every query, in the order the check sent them.
    """

    def __init__(
        self, state: State | None = None, **answers: Callable[..., object]
    ) -> None:
        unknown = set(answers) - set(_METHODS)
        if unknown:
            raise TypeError("FakeSources has no method %s" % ", ".join(sorted(unknown)))
        # No `cfg`: nothing here reaches a URL, and a check reads its config from its own
        # argument, never through the sources.
        self._answers = answers
        self.calls: list[tuple[str, tuple]] = []
        # A zeroed `State` per fake, as the live `Sources` builds one per process: a test that
        # runs a check twice on one fake sees its streak advance, and the next test starts at 0.
        # A test whose two cycles need different answers passes the first fake's `state` on.
        self.state = State() if state is None else state

    def _answer(self, method: str, *args: object, **kwargs: object):
        self.calls.append((method, args))
        if method not in self._answers:
            raise AssertionError(
                "unexpected %s%r: the test gave FakeSources no %s answer"
                % (method, args, method)
            )
        return self._answers[method](*args, **kwargs)

    def queries(self, method: str) -> list[str]:
        """The first argument of every `method` call: the PromQL, LogQL or URL sent."""
        return [args[0] for m, args in self.calls if m == method]

    def prom_scalar(self, promql):
        return self._answer("prom_scalar", promql)

    def prom_vector(self, promql):
        return self._answer("prom_vector", promql)

    def loki_count(self, selector, window):
        return self._answer("loki_count", selector, window)

    def loki_lines(self, logql, window_s, limit):
        return self._answer("loki_lines", logql, window_s, limit)

    def get_json(self, url, headers=None):
        if headers is None:
            return self._answer("get_json", url)
        return self._answer("get_json", url, headers=headers)

    def get_bytes(self, url, headers=None):
        if headers is None:
            return self._answer("get_bytes", url)
        return self._answer("get_bytes", url, headers=headers)

    def post_json(self, url, payload, headers=None):
        if headers is None:
            return self._answer("post_json", url, payload)
        return self._answer("post_json", url, payload, headers=headers)


class FakeSink(Sink):
    """A `Sink` that records each push instead of sending it.

    Attributes:
      pushes: `(token, ok, msg)` for every push, in the order `run_once` sent them.
    """

    def __init__(self) -> None:
        # No `cfg` or opener: nothing here reaches Kuma.
        self.pushes: list[tuple[str, bool, str]] = []

    def push(self, token: str, ok: bool, msg: str) -> None:
        self.pushes.append((token, ok, msg))


def mk(ran: list[str], name: str):
    """A check body that records that it ran, then reports `ok`."""

    def fn(_cfg, _src):
        ran.append(name)
        return True, "%s ok" % name

    return fn


def as_body(result):
    """A check body returning `result`, or raising it when it is an Exception."""
    if isinstance(result, Exception):

        def _body(_cfg, _src):
            raise result
    else:

        def _body(_cfg, _src):
            return result

    return _body
