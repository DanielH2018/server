"""The slice of `airflow.sdk` the DAGs call; Airflow itself is only in the role's image."""

from collections.abc import Callable, Mapping
from typing import overload

class Asset:
    def __init__(self, uri: str) -> None: ...

class XComArg:
    def __rshift__(self, other: XComArg) -> XComArg: ...

@overload
def task(python_callable: Callable[..., object], /) -> Callable[..., XComArg]: ...
@overload
def task(
    *, outlets: list[Asset] = ..., executor_config: Mapping[str, object] = ...
) -> Callable[[Callable[..., object]], Callable[..., XComArg]]: ...
def dag(
    *,
    dag_id: str,
    schedule: object,
    start_date: object,
    catchup: bool = ...,
    max_active_runs: int = ...,
    default_args: Mapping[str, object] = ...,
    tags: list[str] = ...,
) -> Callable[[Callable[[], None]], Callable[[], object]]: ...
