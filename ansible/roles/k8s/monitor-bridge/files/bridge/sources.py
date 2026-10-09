"""Every query a check body sends, and every verdict `run_once` pushes, as two injectable objects.

`cli.main()` builds one `Sources(cfg)` per process and `check.run_once` hands it to every gate
probe and check body beside the frozen `Config`, so a check body reaches its data as
`src.prom_vector(promql)` and never through a module function. A test hands in a fake holding
canned answers (`tests/_fake_sources.py`) instead of patching `bridge.net`.

This is the I/O half of the rule the role's CLAUDE.md states for configuration: a check
ACCEPTS its dependencies rather than looking them up. Before this object existed the suite
stubbed `bridge.net.prom_vector` and its peers on the module, about 150 patches across 29 test
files, each a process-wide mutation that a misspelt name turns into a silent no-op.

The live methods delegate to `bridge.net`, which keeps the transport: the envelope parsing,
the User-Agent, the HTTPError contract. They read `bridge.net.<name>` qualified at call time,
so the Loki parsing tests can still patch `bridge.net._get_json` underneath a real `Sources`.
`cfg` and the `opener` that sends each request are bound once here, so no method takes them; a
test of the delegations hands in a fake opener rather than patching `urllib.request`.

`Sink` is the write half: where `run_once` sends each gate's and each check's verdict. The live
one pushes to Kuma through `bridge.net.push`; a test hands in `tests/_fake_sources.py`'s
`FakeSink`, which records the pushes, instead of patching `bridge.net.push` (#3938).

`log_error_counts` is composed from `loki_vector` and `loki_count` ON THIS CLASS rather than
delegated, so a fake that answers those two primitives answers the composite with the real
query shape instead of a second copy of it.

Stdlib only, like every module under files/.
"""

import bridge.net
from bridge.config import Config
from bridge.net import Opener
from bridge.streaks import State
from bridge.types import JsonValue


class Sources:
    """The live data sources a check body reads, bound to one `Config`.

    Attributes:
      cfg: The configuration holding PROM_URL and LOKI_URL. Read only by the live methods.
      opener: Sends each request, in `urllib.request.urlopen`'s shape. None is the real
        `urlopen`; a test of the delegations passes a fake.
      state: The per-process streak counters and probe caches (`bridge.streaks.State`). Not a
        source: it rides here because `main()` builds this object once and hands it to every
        check body, which is exactly the lifetime the hysteresis needs.
    """

    def __init__(self, cfg: Config, opener: Opener | None = None) -> None:
        self.cfg = cfg
        self.opener = opener
        self.state = State()

    def prom_scalar(self, promql: str) -> float | None:
        """The first series' value of an instant PromQL query, or None when the vector is empty."""
        return bridge.net.prom_scalar(self.cfg, promql, opener=self.opener)

    def prom_vector(self, promql: str) -> list[tuple[dict[str, str], float]]:
        """An instant PromQL query as [(labels, value), ...]; empty when nothing matches."""
        return bridge.net.prom_vector(self.cfg, promql, opener=self.opener)

    def loki_count(self, selector: str, window: str) -> float | None:
        """Total log lines for `selector` over `window`, or None when no stream matches."""
        return bridge.net.loki_count(self.cfg, selector, window, opener=self.opener)

    def loki_vector(self, query: str) -> list[tuple[dict[str, str], float]]:
        """An instant LogQL metric query as [(labels, value), ...]."""
        return bridge.net.loki_vector(self.cfg, query, opener=self.opener)

    def loki_lines(
        self, logql: str, window_s: int, limit: int
    ) -> list[tuple[int, str]]:
        """The raw lines matching `logql` over the last `window_s` seconds, oldest first."""
        return bridge.net.loki_lines(
            self.cfg, logql, window_s, limit, opener=self.opener
        )

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> JsonValue:
        """GET `url` and parse the JSON body. An HTTPError is re-raised with its `code` intact."""
        return bridge.net._get_json(url, headers, opener=self.opener)

    def post_json(
        self, url: str, payload: dict, headers: dict[str, str] | None = None
    ) -> JsonValue:
        """POST `payload` as JSON to `url` and parse the JSON response."""
        return bridge.net._post_json(url, payload, headers, opener=self.opener)

    def log_error_counts(
        self,
        selector: str,
        pattern: str,
        window: str,
        by_label: str = "container",
    ) -> tuple[list[tuple[dict[str, str], float]], float | None]:
        """(matches, total): per-`by_label` counts of `pattern`, and the selector's own volume.

        `total` keeps the fail-open arm honest. A selector that matches no stream returns no
        matches and reads exactly like a healthy estate, which is how HA_BAN_SELECTOR once
        shipped with an `app` label the shipper does not emit and pushed "no ip_ban events"
        through a window holding a real ban. Counting the selector's volume separates "nothing
        is wrong" from "I asked the wrong question".
        """
        matches = self.loki_vector(
            "sum by (%s) (count_over_time(%s |~ `%s` [%s]))"
            % (by_label, selector, pattern, window)
        )
        return matches, self.loki_count(selector, window)


class Sink:
    """Where `run_once` sends each verdict: the Kuma push monitors, bound to one `Config`.

    `cli.main()` builds one per process beside the `Sources`. Pushing is best-effort, as
    `bridge.net.push` documents: an unreachable Kuma is logged, never raised.

    Attributes:
      cfg: The configuration holding KUMA_URL.
      opener: Sends each push, in `urllib.request.urlopen`'s shape. None is the real `urlopen`.
    """

    def __init__(self, cfg: Config, opener: Opener | None = None) -> None:
        self.cfg = cfg
        self.opener = opener

    def push(self, token: str, ok: bool, msg: str) -> None:
        """Push `ok` as status up or down, with `msg`, to the Kuma push monitor for `token`."""
        bridge.net.push(self.cfg, token, ok, msg, opener=self.opener)
