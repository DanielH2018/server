"""Size `-n auto` to the run: one xdist worker per `MODULES_PER_WORKER` test modules selected.

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

## The rule

`workers = min(ceiling, ceil(modules / MODULES_PER_WORKER))`, at least 1. A run that names no
path (the whole suite, from `testpaths`) is left to xdist, which gives it every core. The whole
suite holds about 1,337 modules, so the rule would give it every core anyway. A CI shard holds
about 220 modules on a 4-core runner, so it keeps 4.

`PYTEST_XDIST_AUTO_NUM_WORKERS`, when set, replaces the core count as the ceiling rather than
being the answer. The user-level Claude Code settings set it to 4 for every session, fan-out
batches included, so a batch's one-file run still drops to one worker and its guard set keeps 4.

Two cases leave xdist's own choice alone, by returning None:

- An argument names no file or directory this can see, such as a `--pyargs` module name.
- An argument is the repository root, whose walk would reach `.venv`.

`-n <N>` and `-n0` never reach this hook: xdist asks it only for `auto`.
"""

import math
import os
from pathlib import Path

import pytest

# The guard set's 114 modules ran as fast at 4 workers as at 16, and four files ran fastest on
# one. 15 modules per worker gives those 8 and 1, and `scripts/lib/tests`' 59 modules 4.
MODULES_PER_WORKER = 15


def ceiling() -> int:
    """`PYTEST_XDIST_AUTO_NUM_WORKERS` when it holds a positive number, else the core count."""
    pinned = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "")
    if pinned.isdigit() and int(pinned) > 0:
        return int(pinned)
    return os.process_cpu_count() or os.cpu_count() or 1


def selected_modules(args: list[str], base: Path, root: Path) -> int | None:
    """How many test modules `args` select, or None when one of them cannot be counted.

    Args:
        args: the run's positional arguments, as `config.args` holds them.
        base: the directory relative arguments resolve against.
        root: the repository root, which this refuses to walk.
    """
    count = 0
    for arg in args:
        path = Path(arg.split("::", 1)[0])
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            count += 1
        elif path.is_dir():
            if path.resolve() == root.resolve():
                return None
            count += sum(1 for _ in path.rglob("test_*.py"))
        else:
            return None
    return count


def workers_for(modules: int, cap: int) -> int:
    """The worker count for a run selecting `modules` test modules, at most `cap`."""
    return max(1, min(cap, math.ceil(modules / MODULES_PER_WORKER)))


@pytest.hookimpl(tryfirst=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """The `-n auto` width for this run, or None to leave xdist's own choice."""
    if config.args_source != pytest.Config.ArgsSource.ARGS:
        return None
    modules = selected_modules(
        list(config.args), config.invocation_params.dir, config.rootpath
    )
    if modules is None:
        return None
    return workers_for(modules, ceiling())
