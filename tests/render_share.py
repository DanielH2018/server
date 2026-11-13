"""Let the xdist workers of one pytest run share one render of the fleet and of the k8s tree.

Registered as a plugin from `addopts` in pyproject.toml, beside `leakguard` and `xdist_width`.
The tests import `shared_value`, `placeholder_renderer` and `sharing_placeholder_renders`
from here by bare name, because `tests` is on `pythonpath`.

## Why

`--dist loadscope` keeps a module on one worker, so a module-scoped fixture that renders the
fleet runs once per module. Six modules rendered the same fleet, 10 to 16 seconds each:
the standalone-map, secret-checks-tree, restart and string-values tests, the two
`cluster_vars` tests, and the collision census through `homelab_render.cluster`. The render is
a pure function of the tree, and the tree does not change during a run.

## What is shared

`render_placeholders(tags, keys)` with its default host, roles and image contexts. The first
worker to need a render takes an exclusive `flock`, renders, and pickles the result to a
temporary name. `os.replace` then publishes it under its final name, which is the completion
marker, so a reader never sees a partial file. The others wait on the lock and unpickle the
file. Each call unpickles afresh and returns its own copy: a test edits a render in place, and
no two tests may see each other's edit.

The key is the tags and the keys the caller passed. A call with any other argument bypasses
the cache and renders directly, since another host, roles tree or contexts function changes
the output. The `job_census` module renders the published tags only, which is a different
key, so nothing shares it.

## Lifetime

The cache is per run. The controller reserves the run id at `pytest_configure` and deletes the
run directory then and again at `pytest_sessionfinish`. The first delete clears a directory
left by an earlier run that reused a `--testrunuid`. The second runs after xdist has shut its
workers down, so no worker is still reading. A run killed before that leaves its directory in
the temporary directory, where nothing reads it: the next run has a new id.

Without xdist there is no run id, and `shared_value` keeps the pickled value in the process
instead. No cache survives between runs, and nothing hashes the tree, because a stale cache
would let a test pass while checking nothing.

## The k8s render

`_k8s_render.rendered_docs()` stores its render here too, under `_k8s_render.SHARE_KEY`, so
one worker renders the k8s tree and the rest unpickle it in about 0.01s.

## Who builds the renders

`pytest_collection_finish` builds the k8s render when a collected module imported
`_k8s_render`, and the fleet render when a collected module is a consumer. Every worker runs
it, and `build_each` hands each render to the first worker free to take it. On a leg that
reads both, one worker renders the k8s tree while another renders the fleet, so the workers
wait about one render rather than two in turn (#7710).

## Who pays for the render

DECIDED: both renders leave `--durations`, on purpose. Charged to a test instead, a render
landed on whichever module asked first, which varied by shard and by run, while the serial
`pytest_shard.py --record` run charged every module alone. The shard weight gate then failed
CI on every module that did not pay: eleven modules recorded near 0s measured 5-7.7s in CI
(#4873), and one recorded 11.58s against a measured 0.03s (#5734). Charging every reader
instead would add the render to each of the 70-odd modules that read it. No module's weight
carries a render; `pytest_shard.py --summary` charges them per shard.

Run: uv run pytest tests/repo/test_render_share.py
"""

import contextlib
import fcntl
import hashlib
import json
import os
import pickle
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RUN_ID_ENV = "PYTEST_XDIST_TESTRUNUID"
# The text that marks a test module as a reader of the shared fleet render.
CONSUMER_IMPORT = "from render_share import"

# The pickled values of a run with no xdist run id, by key digest.
_IN_PROCESS: dict[str, bytes] = {}


def run_root(run_id: str | None = None) -> Path | None:
    """The directory shared by every worker of the run `run_id`, or None outside xdist.

    Args:
        run_id: The run's id; None reads the id xdist sets in each worker's environment.
    """
    run_id = run_id or os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    return Path(tempfile.gettempdir()) / f"homelab-pytest-share-{os.getuid()}-{run_id}"


def _private(root: Path) -> Path:
    """`root`, created if need be, after checking that only this user can write to it.

    The cache holds pickles, and unpickling a file another user wrote runs their code.

    Raises:
        RuntimeError: `root` belongs to another user or is writable by group or others.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = root.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o022:
        raise RuntimeError(f"{root} is not private to this user; refusing to read it")
    return root


def shared_value[T](
    key: object, compute: Callable[[], T], *, root: Path | None = None
) -> T:
    """The value `compute` returns, computed once per run and key, and a fresh copy per call.

    Args:
        key: Every input that changes what `compute` returns, as JSON-serialisable data.
        compute: Returns the value to share, which must be picklable.
        root: The shared directory; None derives it from the xdist run id. With neither,
            the value is kept in this process only.

    Raises:
        RuntimeError: `root` is not private to this user.
    """
    root = root or run_root()
    digest = _digest(key)
    if root is None:
        if digest not in _IN_PROCESS:
            _IN_PROCESS[digest] = pickle.dumps(compute())
        return pickle.loads(_IN_PROCESS[digest])
    _publish(root, key, compute, wait=True)
    return pickle.loads((root / f"{digest}.pickle").read_bytes())


def _digest(key: object) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]


def _publish(
    root: Path, key: object, compute: Callable[[], object], *, wait: bool
) -> bool:
    """Publish `compute()`'s pickle under `key` in `root`, unless a worker already has.

    Returns:
        False, having computed nothing, when `wait` is false and another worker holds the
        key's lock. True once the value is published.

    Raises:
        RuntimeError: `root` is not private to this user.
    """
    digest = _digest(key)
    final = _private(root) / f"{digest}.pickle"
    if final.exists():
        return True
    with open(root / f"{digest}.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # Another worker may have published while this one waited on the lock.
        if not final.exists():
            temporary = root / f"{digest}.{os.getpid()}.partial"
            temporary.write_bytes(pickle.dumps(compute()))
            os.replace(temporary, final)
    return True


def build_each(
    builds: list[tuple[object, Callable[[], object]]], *, root: Path | None = None
) -> None:
    """Publish every `(key, compute)` value, starting with the ones no other worker is building.

    A worker that finds a key's lock taken moves on to the next key, and waits only once
    nothing is left to start. So the workers of one run build two renders side by side rather
    than each building both in turn. A `compute` that raises publishes nothing: the first test
    to read that value computes it again and fails naming the test.

    Args:
        builds: The keys and computes, in the order a worker tries them.
        root: As in `shared_value`.
    """
    root = root or run_root()
    if root is None:
        for key, compute in builds:
            with contextlib.suppress(Exception):
                shared_value(key, compute)
        return
    taken = []
    for key, compute in builds:
        with contextlib.suppress(Exception):
            if not _publish(root, key, compute, wait=False):
                taken.append((key, compute))
    for key, compute in taken:
        with contextlib.suppress(Exception):
            _publish(root, key, compute, wait=True)


def placeholder_renderer(real: Callable | None = None) -> Callable:
    """A drop-in for `render_placeholders` that shares the render of its default arguments.

    Args:
        real: The function to share; None imports `render_placeholders`. A patched module
            passes the original, because importing it by name would find this function.
    """
    if real is None:
        from renderer.renderer_lib.placeholders import render_placeholders as real

    def render(tags, keys, *rest, **kwargs):
        if rest or kwargs:
            return real(tags, keys, *rest, **kwargs)
        return shared_value(
            _fleet_key(tags, keys), lambda: real(list(tags), list(keys))
        )

    return render


def _fleet_key(tags, keys) -> list:
    return ["render_placeholders", list(tags), list(keys)]


@contextlib.contextmanager
def sharing_placeholder_renders(*modules: ModuleType) -> Generator[None]:
    """Inside the block, each of `modules` shares the renders of its `render_placeholders`.

    The block patches the name in each module's namespace, which is where a module that
    imported it by name, or a function that imports it at call time, looks it up.
    """
    with pytest.MonkeyPatch.context() as patch:
        for module in modules:
            patch.setattr(
                module,
                "render_placeholders",
                placeholder_renderer(module.render_placeholders),
            )
        yield


def is_consumer(path: Path) -> bool:
    """Whether the test module at `path` imports from here, and so reads the fleet render."""
    try:
        return CONSUMER_IMPORT in path.read_text()
    except OSError, UnicodeDecodeError:
        return False


def fleet_build() -> tuple[list, Callable[[], object]]:
    """The key and compute of the shared fleet render: every render target, every secret key."""
    from renderer.homelab_render import secret_keys
    from renderer.renderer_lib.cluster import select_tags
    from renderer.renderer_lib.placeholders import render_placeholders

    tags, keys = list(select_tags(None)), list(secret_keys())
    return _fleet_key(tags, keys), lambda: render_placeholders(tags, keys)


def prebuilds(
    paths: set[Path],
    modules: Mapping[str, Any] = sys.modules,
    fleet: Callable[[], tuple[list, Callable[[], object]]] = fleet_build,
) -> list[tuple[object, Callable[[], object]]]:
    """The shared renders a run that collected the test modules `paths` reads.

    The k8s render when a collected module imported `_k8s_render`, which is looked up in
    `modules`, not imported: a run that collected nothing reading it, such as `scripts/`
    alone, pays nothing. The fleet render, as `fleet` returns it, when any module in `paths`
    is a consumer.
    """
    builds = []
    if (k8s_render := modules.get("_k8s_render")) is not None:
        builds.append((k8s_render.SHARE_KEY, k8s_render.render))
    if any(is_consumer(path) for path in paths):
        builds.append(fleet())
    return builds


def pytest_collection_finish(session: pytest.Session) -> None:
    """Build the shared renders the run reads before any test's timed phases.

    Under xdist this runs in each worker, which all collect every module, and `build_each`
    spreads the renders over them. Each worker then loads the k8s render into its own cache.
    """
    if session.config.option.collectonly:
        return
    build_each(prebuilds({item.path for item in session.items}))
    if (k8s_render := sys.modules.get("_k8s_render")) is not None:
        k8s_render.prebuild()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Reserve the run's id on the controller, and clear any directory a reused id left."""
    if hasattr(config, "workerinput") or not config.pluginmanager.hasplugin("xdist"):
        return
    if config.getoption("testrunuid", None) is None:
        config.option.testrunuid = uuid.uuid4().hex
    _remove(run_root(config.getoption("testrunuid")))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Delete the run directory once xdist has shut every worker down."""
    config = session.config
    if hasattr(config, "workerinput") or not config.pluginmanager.hasplugin("xdist"):
        return
    _remove(run_root(config.getoption("testrunuid", None)))


def _remove(root: Path | None) -> None:
    if root is not None and root.is_dir() and root.stat().st_uid == os.getuid():
        shutil.rmtree(root, ignore_errors=True)
