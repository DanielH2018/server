"""Paths, task readers and the no-cluster detector the `k8s/volume-revert` guards share.

The guards split five ways -- the drill-proven sequence, the mutation guards,
the snapshot selection, the manifests include, and the seam and input checks that stay in
`test_volume_revert.py` -- and each reads the same task files through these.
"""

import re
from pathlib import Path

from _helpers import REPO as _REPO
from _helpers import load_tasks as _tasks
from _helpers import task_named
from _jinja_env import jinja_env


_ROLE = _REPO / "ansible/roles/k8s/volume-revert"

_CLAIM = _ROLE / "tasks/claim.yml"

_MAIN = _ROLE / "tasks/main.yml"

_DEFAULTS = _ROLE / "defaults/main.yml"

# The two exemption sets the validator skips roles by. scripts/validate/k8s_manifests.py
# re-exports them, but this test parses the source rather than importing it, so it names the
# file that defines them.
_VALIDATOR = _REPO / "scripts/lib/k8s_roles.py"

_GUARD = "not (k8s_no_mutate | bool)"


def _mirrors(name: str, arity: int):
    """Wrap a stand-in so a call with more than `arity` arguments fails naming the stand-in.

    Each stand-in mirrors ansible-core 2.22 only for the arguments the role passes today. A
    flag or a group argument would make it diverge from what `ansible/volume-revert.yml` runs.
    """

    def wrap(fn):
        def call(*args, **kwargs):
            if kwargs or len(args) > arity:
                raise NotImplementedError(
                    f"the `{name}` stand-in in _volume_revert.py mirrors ansible-core only "
                    f"for {arity - 1} argument(s), and the role now passes {args[1:]!r} "
                    f"{kwargs!r}; extend the stand-in to match ansible-core 2.22"
                )
            return fn(*args)

        return call

    return wrap


@_mirrors("regex_escape", 1)
def _regex_escape(value) -> str:
    return re.escape(str(value))


@_mirrors("regex_search", 2)
def _regex_search(value, pattern: str) -> str | None:
    """The matched text, or None, as `regex_search` returns with no group arguments."""
    found = re.search(pattern, str(value))
    return found.group() if found else None


@_mirrors("split", 2)
def _split(value, sep: str | None = None) -> list[str]:
    return str(value).split(sep)


@_mirrors("match", 2)
def _match(value, pattern: str) -> bool:
    return re.match(pattern, str(value)) is not None


def _render(expression: str, **context):
    """Render one of the role's expressions, with stand-ins that mirror ansible-core 2.22's forms.

    The renderer's filter set lacks `regex_escape`, `regex_search`, `split` and the `match` test,
    because no rendered template uses them; only this role's tasks do, and H7.55 deletes those.
    """
    env = jinja_env()
    env.filters["regex_escape"] = _regex_escape
    env.filters["regex_search"] = _regex_search
    env.filters["split"] = _split
    env.tests["match"] = _match
    return env.from_string(expression).render(**context)


def _task_names(path: Path) -> list[str]:
    return [str(task.get("name", "")) for task in _tasks(path)]


def _named(path: Path, fragment: str) -> dict:
    return task_named(_tasks(path), fragment)


def _index(names: list[str], fragment: str) -> int:
    """The position of the ONE task whose name contains `fragment`.

    Unique-match, not first-match, and that is the whole point. An ordering assert built on
    first-match is satisfied by any earlier task that happens to share the substring, so
    renaming a read task to mention `disableFrontend` would make the assert below pass while
    the assert task itself sat after the revert. This repo has shipped six tests that credit
    the argument against the thing; refusing an ambiguous match is how this one avoids being
    the seventh.
    """
    hits = [i for i, name in enumerate(names) if fragment in name]
    if len(hits) != 1:
        raise AssertionError(
            f"{fragment!r} matches {len(hits)} task names in the file under test; an ordering "
            f"assert can only pin a unique task. Rename so exactly one task carries it."
        )
    return hits[0]


def _guard_of(task: dict) -> list[str]:
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return [str(condition).strip() for condition in conditions]
