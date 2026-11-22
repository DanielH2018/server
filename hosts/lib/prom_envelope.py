"""The envelope of a Prometheus HTTP API answer, for the host scripts that query Prometheus.

The builder's rollout gate and the triage poller each ship a copy beside their script (a `lib:`
entry in their `component.yml`) and import it by bare name. Stdlib only.

Typical usage example:

    from prom_envelope import PromAnswerError, prom_result

    for series in prom_result(json.loads(body)):
        print(series.get("metric"))
"""


class PromAnswerError(ValueError):
    """A Prometheus answer that is not a successful `{data: {result: [...]}}` envelope."""


def prom_result(body: object) -> list[dict]:
    """The `data.result` objects of a successful Prometheus answer.

    An absent `data` or `result` reads as no series, as monitor-bridge's
    `bridge/net.py:_query_result` reads it.

    Raises:
        PromAnswerError: `body` is not an object, its status is not `success`, its `data` is
            not an object, or `result` is not a list of objects. A `"data": null` or
            `"result": null` raises rather than reading as no series.
    """
    if not isinstance(body, dict):
        raise PromAnswerError(f"the answer is not a JSON object: {body!r:.80}")
    if body.get("status") != "success":
        raise PromAnswerError(
            f"the answer's status is {body.get('status')!r}: {body.get('error')}"
        )
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise PromAnswerError(f"the answer's data is not an object: {data!r:.80}")
    result = data.get("result", [])
    if not isinstance(result, list):
        raise PromAnswerError(f"the answer's result is not a list: {result!r:.80}")
    for series in result:
        if not isinstance(series, dict):
            raise PromAnswerError(f"a series is not an object: {series!r:.80}")
    return result
