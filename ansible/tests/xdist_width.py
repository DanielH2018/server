"""Size `-n auto` to the run: one xdist worker per heaviest module's worth of recorded seconds.

Registered as a plugin from `addopts` in pyproject.toml, beside `-n auto`, so it reaches
every run that does not pass its own `-n`.

## Why

xdist's own `-n auto` starts one worker per core whatever the run selects. Each worker is its
own interpreter that imports and collects the whole selection, so a run of one or two files
on daniel-box started 16 interpreters to run them. Measured on daniel-box on 2026-10-18, each
selection in its own user scope (memory.peak, which counts page cache too):

| Selection                        | Modules | 1 worker      | 4 workers     | 16 workers     |
|----------------------------------|---------|---------------|---------------|----------------|
| four `scripts/dev/tests` files   | 4       | 106 MiB, 1.4s | 225 MiB, 1.7s | 693 MiB, 2.8s  |
| `scripts/lib/tests`              | 59      | 262 MiB, 46s  | 458 MiB, 18s  | 1190 MiB, 27s  |
| the fan-out brief's guard set    | 114     | 2342 MiB, 163s| 2655 MiB, 48s | 3729 MiB, 47s  |

Past the width a selection can use, more workers cost memory and buy nothing. Fan-out batches
ran 3,473 targeted pytest runs in the week to 2026-10-18 at a median of 3.8s, each at the four
workers the session ceiling below allows, where most needed one. A run outside a Claude
session, such as a hand-typed one on daniel-box, got all 16. That is what this plugin narrows
(#5667's follow-up).

The first rule counted modules, one worker per 15, so a few heavy modules ran on one worker
(#5967). The five modules of the `static-ratchet-tests-python` prek hook took 13.3-16.6s under
`-n auto` and 6.7-8.5s under `-n 4` (#5917). Re-measured on daniel-server (8 cores) on
2026-10-25, two rounds interleaved at load average 8.5-11.8, wall seconds per run:

| Selection                       | Modules | Recorded | Heaviest | 1       | 2       | 3     | 4     | 5  | 8     |
|---------------------------------|---------|----------|----------|---------|---------|-------|-------|----|-------|
| the `-python` hook's five       | 5       | 5.7s     | 2.22s    | 16-20   | 15-18   | 10-11 |       | 12 |       |
| `scripts/lib/tests`             | 66      | 32.1s    | 8.07s    |         | 52-54   |       | 41-49 |    | 49-50 |
| four light `tests/` files       | 4       | 0.3s     | 0.27s    | 2.3-4.5 | 2.0-2.1 |       |       |    |       |

## The rule

Under `--dist loadscope` a module's tests stay on one worker, so no width finishes the run
before its heaviest module does. Workers beyond `recorded seconds / heaviest module` only idle,
and fewer leave the heaviest module waiting behind others. So:

`workers = min(ceiling, ceil(total / max(heaviest, MIN_SECONDS_PER_WORKER)))`, at least 1.

That gives the first two rows above 3 and 4, the fastest width measured for each. It gives the
third 1, where a second worker saved under half a second in one round of two. The seconds
come from `scripts/dev/pytest_shard_weights.json`, which the shard split already keeps current.
A module the table does not hold is charged the table's mean. A run that names no path (the
whole suite, from `testpaths`) is left to xdist, which gives it every core. A CI shard holds
about 220 modules, around 240 recorded seconds, on a 4-core runner, so it keeps 4.

`PYTEST_XDIST_AUTO_NUM_WORKERS`, when set, replaces the core count as the ceiling rather than
being the answer. The user-level Claude Code settings set it to 4 for every session, fan-out
batches included, so a batch's one-file run still drops to one worker and its guard set keeps 4.

Three cases leave xdist's own choice alone, by returning None:

- An argument names no file or directory this can see, such as a `--pyargs` module name.
- An argument is the repository root, whose walk would reach `.venv`.
- The weight table is missing or unreadable.

`-n <N>` and `-n0` never reach this hook: xdist asks it only for `auto`.
"""

import json
import math
import os
import statistics
from pathlib import Path

import pytest

# Below this many recorded seconds a worker costs more to start than it saves: four files
# recorded at 0.3s ran in 1.4s on one worker and 1.7s on four (the 2026-10-18 table).
MIN_SECONDS_PER_WORKER = 2.0

WEIGHTS_TABLE = Path("scripts/dev/pytest_shard_weights.json")


def ceiling() -> int:
    """`PYTEST_XDIST_AUTO_NUM_WORKERS` when it holds a positive number, else the core count."""
    pinned = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "")
    if pinned.isdigit() and int(pinned) > 0:
        return int(pinned)
    return os.process_cpu_count() or os.cpu_count() or 1


def selected_modules(args: list[str], base: Path, root: Path) -> list[Path] | None:
    """The test modules `args` select, resolved and deduplicated, or None when one cannot be.

    Args:
        args: the run's positional arguments, as `config.args` holds them.
        base: the directory relative arguments resolve against.
        root: the repository root, which this refuses to walk.
    """
    found: set[Path] = set()
    for arg in args:
        path = Path(arg.split("::", 1)[0])
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            found.add(path.resolve())
        elif path.is_dir():
            if path.resolve() == root.resolve():
                return None
            found.update(p.resolve() for p in path.rglob("test_*.py"))
        else:
            return None
    return sorted(found)


def load_weights(root: Path) -> dict[str, float] | None:
    """The recorded seconds per module under `root`, or None when the table cannot be read."""
    try:
        table = json.loads((root / WEIGHTS_TABLE).read_text())
        return {str(k): float(v) for k, v in table.items()}
    except OSError, ValueError, AttributeError:
        return None


def module_seconds(
    modules: list[Path], root: Path, weights: dict[str, float]
) -> list[float]:
    """Each module's recorded seconds, keyed by its path under `root`; unrecorded get the mean."""
    unrecorded = statistics.fmean(weights.values()) if weights else 0.0
    seconds = []
    for module in modules:
        try:
            key = module.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            key = None
        seconds.append(weights.get(key, unrecorded) if key else unrecorded)
    return seconds


def workers_for(seconds: list[float], cap: int) -> int:
    """The worker count for a run whose modules record `seconds`, at most `cap`."""
    if not seconds:
        return 1
    per_worker = max(max(seconds), MIN_SECONDS_PER_WORKER)
    return max(1, min(cap, math.ceil(sum(seconds) / per_worker)))


@pytest.hookimpl(tryfirst=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """The `-n auto` width for this run, or None to leave xdist's own choice."""
    if config.args_source != pytest.Config.ArgsSource.ARGS:
        return None
    root = config.rootpath
    modules = selected_modules(list(config.args), config.invocation_params.dir, root)
    weights = load_weights(root)
    if modules is None or weights is None:
        return None
    return workers_for(module_seconds(modules, root, weights), ceiling())
