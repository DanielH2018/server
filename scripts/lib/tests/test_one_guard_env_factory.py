"""The render guards build their Jinja environment through one factory (#8182).

`render_guard.make_env` and `ansible_jinja_env.make_ansible_env` were two factories for the
same environment. Whichever one survives, the other is defined nowhere and called nowhere.

Run: uv run pytest scripts/lib/tests/test_one_guard_env_factory.py
"""

import re

from lib.proc_testing import run
from lib.repo_paths import REPO

FACTORIES = ("make_env", "make_ansible_env")


def _tracked_python() -> dict[str, str]:
    names = run(["git", "ls-files", "-z", "*.py"], cwd=REPO, check=True).stdout.split(
        "\0"
    )
    return {name: (REPO / name).read_text() for name in names if name}


def test_one_guard_env_factory_remains_and_nothing_names_the_deleted_one():
    sources = _tracked_python()
    assert "scripts/lib/render_guard.py" in sources
    defined = {
        name: sorted(
            path
            for path, text in sources.items()
            if re.search(rf"^def {name}\(", text, re.MULTILINE)
        )
        for name in FACTORIES
    }
    survivors = [name for name, paths in defined.items() if paths]
    assert len(survivors) <= 1, f"more than one guard env factory is defined: {defined}"
    for name in FACTORIES:
        if name in survivors:
            continue
        stale = sorted(
            path for path, text in sources.items() if re.search(rf"\b{name}\b", text)
        )
        assert not stale, f"{name} is deleted but still named in: {stale}"
