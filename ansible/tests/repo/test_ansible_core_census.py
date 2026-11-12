"""H7's two censuses for removing ansible-core (`docs/target-architecture/host/h7-delete-ansible.md`).

- The deletion census, in two halves. A subprocess collects every `testpaths` entry while
  `_collect_without_ansible.py` refuses each `import ansible`, which catches an import a
  helper module makes on a test's behalf. Collection runs no test body, and the refusal sees
  only `__import__`, so a grep half also reads every tracked `*.py` for an import statement,
  an `import_module` or a `find_spec` naming `ansible`. A file outside `ALLOW` that either
  half finds fails it. `ALLOW` is empty, because H7.56 removed ansible-core from the
  environment, so an import of it now fails wherever it runs. An import held in a
  string a test runs in a subprocess reads as an import to the grep, so `IN_A_STRING` names
  each such file.
- The filter census. Every filter and test name a tracked `*.j2` uses is registered in the
  renderer's strict environment: Jinja's built-ins, `renderer_lib/jinja_filters.py` and the
  repo's filter plugins. `_jinja_env.jinja_env` renders on that set.

The deletion census runs a 40-second collection, so it is not a row of
`test_census_rows_python.py`, which prek runs at every commit.

Run: uv run pytest ansible/tests/repo/test_ansible_core_census.py
"""

import json
import re
import sys
from pathlib import Path

import pytest
from jinja2 import nodes

from _helpers import REPO
from _row_table import tracked
from lib.proc_testing import run
from lib.render_guard import SHARED_TPL
from renderer.renderer_lib.environment import strict_env

CHILD = Path(__file__).with_name("_collect_without_ansible.py")
ALLOW: dict[str, str] = {}
# The grep half's named member: an import held in a string.
ANSIBLE_FREE = "scripts/renderer/tests/test_renderer_ansible_free.py"
IN_A_STRING = {
    ANSIBLE_FREE: "the shim of its red half",
    "ansible/tests/repo/test_ansible_core_census.py": "this census's own red fixtures",
}
IMPORT_LINE = re.compile(
    r"^\s*(from|import) ansible\b"
    r"""|(import_module|find_spec)\(\s*["']ansible\b""",
    re.MULTILINE,
)
TIMEOUT = 600


def _collect(tmp_path: Path, *pytest_args: str) -> dict[str, list[str]]:
    out = tmp_path / "importers.json"
    done = run(
        [sys.executable, str(CHILD), str(out), ",".join(ALLOW), *pytest_args],
        cwd=REPO,
        check=False,
        timeout=TIMEOUT,
    )
    assert out.exists(), done.stdout + done.stderr
    return json.loads(out.read_text())


def test_the_suite_collects_with_ansible_core_refused_outside_the_allowlist(tmp_path):
    importers = _collect(tmp_path)
    offenders = {rel: names for rel, names in importers.items() if rel not in ALLOW}
    assert not offenders, f"these files import ansible-core: {offenders}"


def importers_by_text(paths: list[str]) -> list[str]:
    """Each file in `paths` whose text names `ansible` in an import."""
    return [rel for rel in paths if IMPORT_LINE.search((REPO / rel).read_text())]


def test_no_tracked_python_names_ansible_core_in_an_import_outside_the_allowlist():
    found = importers_by_text(tracked("*.py"))
    assert ANSIBLE_FREE in found, (
        "the grep no longer finds test_renderer_ansible_free.py's import"
    )
    offenders = sorted(set(found) - set(ALLOW) - set(IN_A_STRING))
    assert not offenders, f"these files import ansible-core: {offenders}"


def test_every_allowlisted_file_still_imports_ansible_core():
    """An entry whose import is gone is deleted, so the list empties as H7 lands."""
    listed = [*ALLOW, *IN_A_STRING]
    stale = sorted(set(listed) - set(importers_by_text(listed)))
    assert not stale, f"these no longer import ansible; drop them from ALLOW: {stale}"


@pytest.mark.parametrize(
    "text",
    [
        "def f():\n    from ansible.template import Templar\n",
        "mod = importlib.import_module('ansible.constants')\n",
        'spec = importlib.util.find_spec("ansible.cli.playbook")\n',
    ],
    ids=["deferred-import", "import-module", "find-spec"],
)
def test_an_import_the_collection_cannot_see_is_flagged(tmp_path, text):
    path = tmp_path / "mod.py"
    path.write_text(text)
    assert importers_by_text([str(path)]) == [str(path)]


def test_a_module_that_names_ansible_in_no_import_is_clean(tmp_path):
    path = tmp_path / "mod.py"
    path.write_text('ANSIBLE = REPO / "ansible"\nimport ansible_lint_rules\n')
    assert importers_by_text([str(path)]) == []


def test_a_module_that_imports_ansible_core_is_flagged(tmp_path):
    module = tmp_path / "test_imports_ansible.py"
    module.write_text("import ansible.errors\n\n\ndef test_nothing():\n    pass\n")
    importers = _collect(tmp_path, str(module))
    assert importers.get(str(module)) == ["ansible.errors"]


def unregistered(paths: list[Path]) -> dict[str, list[str]]:
    """Each `filter <name>` or `test <name>` the strict environment lacks, with its templates."""
    env = strict_env(SHARED_TPL)
    missing: dict[str, list[str]] = {}
    for path in paths:
        for node in env.parse(path.read_text()).find_all((nodes.Filter, nodes.Test)):
            kind, table = (
                ("filter", env.filters)
                if isinstance(node, nodes.Filter)
                else ("test", env.tests)
            )
            if node.name.removeprefix("ansible.builtin.") not in table:
                missing.setdefault(f"{kind} {node.name}", []).append(str(path))
    return missing


def _templates() -> list[Path]:
    return [REPO / rel for rel in tracked("*.j2")]


def test_every_filter_and_test_a_template_uses_is_registered():
    assert unregistered(_templates()) == {}


def test_the_filter_census_reads_a_template_that_uses_regex_replace():
    """The plan names `b64encode`, which no tracked template uses."""
    env = strict_env(SHARED_TPL)
    assert any(
        node.name == "regex_replace"
        for path in _templates()
        for node in env.parse(path.read_text()).find_all(nodes.Filter)
    )


def test_a_template_naming_an_unregistered_filter_is_flagged(tmp_path):
    template = tmp_path / "bad.yaml.j2"
    template.write_text("value: {{ name | no_such_filter }}\n")
    assert unregistered([template]) == {"filter no_such_filter": [str(template)]}
