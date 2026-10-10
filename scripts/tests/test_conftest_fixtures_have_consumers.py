"""`scripts/conftest.py` carries no fixture that no test requests (#4337).

#4332 dropped `plan()`'s `resolve_ip` parameter, which was the only consumer of the fake
resolver fixture and its private helper. A fixture nobody requests reads as
live scaffolding, so the next reader keeps it alive by copying it.
"""

import subprocess
from pathlib import Path

from lib.repo_paths import REPO

# Built by concatenation so this file does not itself match the census below.
_TOKEN = "fake_" + "resolve"
_SCRIPTS_CONFTEST = REPO / "scripts" / "conftest.py"


def test_no_tracked_file_under_scripts_or_ansible_names_the_resolver_fixture():
    # The issue's Verify-by: a grep for the fixture's name over scripts/ and ansible/ is empty.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "scripts", "ansible"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split("\0")
    this_file = Path(__file__).resolve()
    hits = []
    for rel in filter(None, tracked):
        path = REPO / rel
        if path.resolve() == this_file or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += [
            f"{rel}:{n}"
            for n, line in enumerate(text.splitlines(), 1)
            if _TOKEN in line
        ]
    assert hits == [], f"{_TOKEN} has no consumer since #4332; still named at: {hits}"


def test_scripts_conftest_defines_neither_the_resolver_fixture_nor_its_helper(request):
    # Read the conftest module pytest actually loaded, rather than its source text.
    loaded = [
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None)
        and Path(plugin.__file__).resolve() == _SCRIPTS_CONFTEST.resolve()
    ]
    assert len(loaded) == 1, f"expected pytest to load {_SCRIPTS_CONFTEST} once"
    conftest = loaded[0]
    leftovers = [name for name in (_TOKEN, "_" + _TOKEN) if hasattr(conftest, name)]
    assert leftovers == [], f"scripts/conftest.py still defines {leftovers}"
