"""The API-server and Prometheus answers a Flux Job script reads, parsed into typed values.

A Job script carries this file beside it as a byte-identical copy, as it carries
`flux_job_connect.py`; the Job census in `scripts/renderer/tests/_job_census.py` holds both.
Standard library only: the Job image is `python:3.15-alpine`.

Typical usage example:

    from flux_job_parse import EndpointState, endpoint_states, parse_prom_vector

    serving = endpoint_states(slice_list["items"]).count(EndpointState.READY)
    for series in parse_prom_vector(body):
        print(series.labels.get("job"), series.value)
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "EndpointState",
    "Series",
    "endpoint_states",
    "parse_prom_result",
    "parse_prom_vector",
]


class EndpointState(Enum):
    """What an EndpointSlice endpoint's `conditions` say about it.

    The API reads an absent `ready` as true, so a caller that follows the API counts `UNSTATED`
    as ready, and a caller that needs an explicit answer counts `READY` alone.
    """

    READY = "ready"
    UNSTATED = "unstated"
    NOT_READY = "not-ready"
    TERMINATING = "terminating"

    @classmethod
    def of(cls, endpoint: Mapping) -> "EndpointState":
        """The state of one entry of an EndpointSlice's `endpoints` list."""
        conditions = endpoint.get("conditions") or {}
        if conditions.get("terminating") is True:
            return cls.TERMINATING
        ready = conditions.get("ready")
        if ready is True:
            return cls.READY
        if ready is False:
            return cls.NOT_READY
        return cls.UNSTATED


def endpoint_states(items: Sequence[Mapping]) -> list[EndpointState]:
    """The state of every endpoint in the EndpointSlices `items`.

    A Service with no ready pod keeps its EndpointSlice and sets `endpoints` to null, which
    contributes no state.
    """
    return [
        EndpointState.of(endpoint)
        for item in items
        for endpoint in item.get("endpoints") or []
    ]


@dataclass(frozen=True)
class Series:
    """One instant-vector sample: its label set and its value."""

    labels: dict[str, str]
    value: float


def parse_prom_result(body: object) -> list[dict]:
    """The series objects of a Prometheus query's answer, instant or range.

    An absent `data` or `result` reads as no series, as monitor-bridge's
    `bridge/net.py:_query_result` reads it.

    Raises:
        ValueError: `body` is not a `{status: success, data: {result: [...]}}` envelope, or a
            series is not an object. A `"data": null` or `"result": null` raises here rather
            than reading as no series. A failed query's message carries Prometheus's `error`.
    """
    if not isinstance(body, dict):
        raise ValueError(f"the answer is not a JSON object: {body!r:.80}")
    if body.get("status") != "success":
        raise ValueError(
            f"the answer's status is {body.get('status')!r}: {body.get('error')}"
        )
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise ValueError(f"the answer's data is not an object: {data!r:.80}")
    result = data.get("result", [])
    if not isinstance(result, list):
        raise ValueError(f"the answer's result is not a list: {result!r:.80}")
    for series in result:
        if not isinstance(series, dict):
            raise ValueError(f"a series is not an object: {series!r:.80}")
    return result


def parse_prom_vector(body: object) -> list[Series]:
    """The samples of a Prometheus instant query's answer.

    Raises:
        ValueError: `parse_prom_result` refuses `body`, or a sample is not a
            `{metric, value: [time, "number"]}` object.
    """
    return [_series(sample) for sample in parse_prom_result(body)]


def _series(sample: dict) -> Series:
    metric = sample.get("metric") or {}
    pair = sample.get("value")
    if not isinstance(metric, dict) or not isinstance(pair, list) or len(pair) != 2:
        raise ValueError(
            f"a sample is not {{metric, value: [time, number]}}: {sample!r:.80}"
        )
    try:
        value = float(pair[1])
    except (TypeError, ValueError) as err:
        raise ValueError(f"a sample's value {pair[1]!r} is not a number") from err
    return Series({k: v for k, v in metric.items() if isinstance(v, str)}, value)
