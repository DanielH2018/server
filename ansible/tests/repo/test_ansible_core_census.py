"""H7's two censuses for removing ansible-core (`docs/target-architecture/host/h7-delete-ansible.md`).

- The deletion census. A subprocess collects every `testpaths` entry while
  `_collect_without_ansible.py` refuses each `import ansible`. An import from a file outside
  `ALLOW` fails it. Each `ALLOW` entry names the H7 step that removes its import, and the list
  must be empty before H7.56, which removes ansible-core, merges.
- The filter census. Every filter and test name a tracked `*.j2` uses is registered in the
  renderer's strict environment: Jinja's built-ins, `renderer_lib/jinja_filters.py` and the
  repo's filter plugins. H7.54 rebuilds `_helpers.py`'s env on that set.

The deletion census runs a 40-second collection, so it is not a row of
`test_census_rows_python.py`, which prek runs at every commit.

Run: uv run pytest ansible/tests/repo/test_ansible_core_census.py
"""

import json
import re
import sys
from pathlib import Path

from jinja2 import nodes

from _helpers import REPO
from _row_table import tracked
from lib.proc_testing import run
from lib.render_guard import SHARED_TPL
from renderer.renderer_lib.environment import strict_env

CHILD = Path(__file__).with_name("_collect_without_ansible.py")
ALLOW = {
    "ansible/tests/_helpers.py": "H7.54 rebuilds the test env on the renderer's filters",
    "scripts/deploy_tools/deploy_lib/flags.py": "H7.53 deletes the deployer's PlaybookCLI read",
    "ansible/tests/setup/test_pi_escalation_timeout.py": "H7.55 deletes it with the playbooks",
}
IMPORT_LINE = re.compile(r"^\s*(from|import) ansible\b", re.MULTILINE)
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
    assert "ansible/tests/_helpers.py" in importers, (
        "the collection never reached _helpers.py"
    )
    offenders = {rel: names for rel, names in importers.items() if rel not in ALLOW}
    assert not offenders, f"these files import ansible-core: {offenders}"


def test_every_allowlisted_file_still_imports_ansible_core():
    """An entry whose import is gone is deleted, so the list empties as H7 lands."""
    stale = [rel for rel in ALLOW if not IMPORT_LINE.search((REPO / rel).read_text())]
    assert not stale, f"these no longer import ansible; drop them from ALLOW: {stale}"


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
