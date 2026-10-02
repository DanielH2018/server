"""A test renders Jinja and finds the repo root through the shared helpers, never by hand.

Three things a test module does not re-derive for itself:

1. A Jinja environment. A bare `jinja2.Environment()` has none of Ansible's whitespace flags
   and none of its filters, so a template renders under it differently from a deploy: a
   `{% for %}` leaves a blank line per iteration, and `| bool` fails or means Python's
   `bool()`. Twenty-seven tests built one, each choosing its own subset of the flags. The
   sanctioned paths are `lib.ansible_jinja_env.make_ansible_env` / `template_env` for a
   template's text, and `ansible/tests/_helpers.py`'s `jinja_env` / `render_expr` for a task
   expression that yields a structure.
2. The repo root. Forty-seven test modules computed it from `Path(__file__)` under three
   names, where `lib.repo_paths.REPO` already is that path.
3. A shell template's text. A guard that reads a `*.sh.j2`'s SOURCE matches `{{ ... }}` the
   moment a value it asserts on — a retry count, a URL, a threshold — moves into a role default,
   and a pattern that matches nothing passes (#3178). `ansible/tests/_shell_render.py` renders
   the whole roster through the same gate that shellchecks it, so an assertion cannot drift from
   what the host runs. Rule 3 catches the four spellings a deliberate reader takes: a path
   expression ending in a `*.sh.j2` name, a `glob("*.sh.j2")` roster, a loop over a
   template RESOLVER — `iter_cron_targets`, `cron_job_scripts`, `discover_templates` — whose
   module names no `*.sh.j2` anywhere (#3200), including the resolver result a pytest fixture
   hands its consumers as a parameter (#3206), and a read inside a helper the module itself
   defines, which the call to that helper stands in for (#3220). It does NOT catch a census wider than that:
   `test_healthchecks_pings.py` reaches every template through an `rglob("*")`, so the rule
   cannot see it either way. That module routes its `*.sh.j2` reads
   through `rendered_shell_text` of its own accord (#3190), and its own
   `test_a_shell_template_is_read_rendered_not_as_source` is what holds it there.

This is the sibling of `test_scratch_repos_go_through_git_testing.py`, which holds the same
line for scratch git repositories.

Every rule is read from the AST, so a docstring naming the old form is prose rather than a
hit. The repo-root rule evaluates the path arithmetic against the module's own location, so it
flags `parents[3]` in one directory and passes the same text where it lands elsewhere.

The detectors themselves live in `_render_helper_rules.py`; this module states the rules, holds
the exemption maps and runs each detector over the tree.

Run: uv run pytest scripts/tests/test_tests_share_render_and_path_helpers.py
"""

from pathlib import Path

from _render_helper_rules import (
    bare_jinja_envs,
    local_repo_roots,
    shell_template_source_reads,
)
from lib.repo_paths import ANSIBLE, REPO, SCRIPTS
from test_script_bootstraps_present import is_pytest_only

# Every `testpaths` entry that holds Python, so the rule covers what pytest collects.
_TEST_ROOTS = (
    SCRIPTS,
    ANSIBLE / "tests",
    ANSIBLE / "roles",
    REPO / ".claude" / "hooks",
    REPO / ".claude" / "tests",
    REPO / "evals",
)

# Modules allowed a bare Jinja environment, each with the reason it is not an Ansible render.
JINJA_EXEMPT = {
    # The sanctioned expression helper itself: `jinja_env` is the NativeEnvironment every
    # other test borrows.
    "ansible/tests/_helpers.py",
    # Renders Home Assistant's Jinja, not Ansible's. Ansible's filters would be wrong there.
    "ansible/roles/k8s/home-assistant/tests/jinja_harness.py",
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the tree is clean.
KNOWN_MEMBERS = frozenset(
    {
        ".claude/hooks/tests/test_hook_scripts_executable.py",
        "ansible/roles/k8s/monitor-bridge/tests/test_check_ha.py",
        "ansible/tests/k8s/_manifest_guards.py",
        "ansible/tests/longhorn/_restore_drill.py",
        "ansible/tests/longhorn/test_longhorn_reap_guard.py",
        "ansible/tests/setup/test_kuma_check_timer.py",
        "ansible/tests/staging/test_staging_network.py",
        "scripts/deploy_tools/tests/test_deploy_exit_codes.py",
        "scripts/docs/tests/test_mkdocs_repo_links.py",
    }
)


def _test_modules() -> list[Path]:
    found = []
    for root in _TEST_ROOTS:
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO)
            if "collections" not in rel.parts and is_pytest_only(rel):
                found.append(path)
    return sorted(set(found))


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


# Test modules that still read a `*.sh.j2`'s source, each with the reason a render cannot
# answer the question it asks. #3189 converted the other sixteen; each entry here is permanent —
# the subject of each is something a render erases, so there is nothing left to convert.
# `test_every_grandfathered_reader_still_reads_one` drops an entry that no longer reads one, and
# the rule then refuses it coming back.
#
# The list GREW in #3200, and that is the one way it may: the detector learned to see a
# read it had been blind to, so a module that was already reading a source joined the list the
# moment it was no longer invisible. A conversion never adds an entry. #3206 widened the
# detector the same way — a resolver result reached through a fixture parameter — and added no
# entry, because the one module with such a fixture was already listed. #3220 widened it to a
# read inside a local helper and added no entry either: the one module reading that way,
# `ansible/tests/deploy/test_setup_drift_check.py`, was converted in the same change.
SHELL_SOURCE_READERS = {
    "ansible/tests/deploy/test_setup_render_manifest.py": (
        "hashes the template's raw BYTES — a trailing newline and a truncated-read "
        "comparison, neither of which survives a render"
    ),
    "scripts/validate/tests/test_shell_template_cron_rules.py": (
        "hands a cron rule its own second argument — the rule under test takes the template "
        "TEXT, and `shell_templates.py` reads it off disk the same way in production, so a "
        "render would test a different call than the gate makes"
    ),
}


def test_the_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_exemption_still_names_a_file_that_exists():
    """A stale exemption silently lets a whole module back out of the rule.

    Covers the grandfathered readers too: a deleted module left in that list makes
    `test_every_grandfathered_reader_still_reads_one` raise `FileNotFoundError` instead of
    naming the entry to drop.
    """
    listed = set(JINJA_EXEMPT) | set(SHELL_SOURCE_READERS)
    gone = sorted(name for name in listed if not (REPO / name).exists())
    assert gone == [], f"exempted modules that no longer exist: {gone}"


def test_no_test_module_builds_a_bare_jinja_environment():
    offenders = {
        _rel(p): envs
        for p in _test_modules()
        if _rel(p) not in JINJA_EXEMPT and (envs := bare_jinja_envs(p.read_text()))
    }
    assert offenders == {}, (
        "these tests build their own Jinja environment, which renders a template without "
        "Ansible's whitespace flags and filters. Use `lib.ansible_jinja_env.make_ansible_env` "
        "(pass `undefined_cls=` to keep a strict render) or `_helpers.jinja_env` for a "
        f"structure-valued expression: {offenders}"
    )


def test_no_test_module_computes_the_repo_root():
    offenders = {
        _rel(p): names
        for p in _test_modules()
        if (names := local_repo_roots(p.read_text(), p.resolve()))
    }
    assert offenders == {}, (
        "these tests derive the repo root from `__file__`; import `REPO` from "
        f"`lib.repo_paths` instead (`REPO as _REPO` keeps a local name): {offenders}"
    )


def test_the_shared_helpers_are_clean():
    assert (
        bare_jinja_envs(
            "from lib.ansible_jinja_env import make_ansible_env\nmake_ansible_env()\n"
        )
        == []
    )
    assert local_repo_roots("from lib.repo_paths import REPO\n", REPO / "t.py") == []


def test_a_bare_jinja_environment_is_flagged_in_each_spelling():
    assert bare_jinja_envs("from jinja2 import Environment\nEnvironment()\n") == [
        "Environment"
    ]
    assert bare_jinja_envs("import jinja2\njinja2.Environment(trim_blocks=True)\n") == [
        "jinja2.Environment"
    ]
    assert bare_jinja_envs(
        "from jinja2.nativetypes import NativeEnvironment as N\nN()\n"
    ) == ["N"]


def test_another_template_language_is_not_flagged():
    """`liquid.Environment` renders Kuma's notification bodies, not an Ansible template."""
    assert bare_jinja_envs("import liquid\nliquid.Environment()\n") == []
    assert bare_jinja_envs('"""jinja2.Environment() used to be built here."""\n') == []


def test_a_local_repo_root_is_flagged_in_each_spelling(tmp_path):
    module = tmp_path / "scripts" / "x" / "tests" / "test_y.py"
    for source in (
        "REPO = Path(__file__).resolve().parents[3]\n",
        "_REPO: Path = Path(__file__).parent.parent.parent.parent\n",
        "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname("
        "os.path.abspath(__file__)))))\n",
    ):
        assert local_repo_roots(source, module, repo=tmp_path) != [], source


def test_a_path_short_of_the_repo_root_is_not_flagged(tmp_path):
    """`parents[1]` is the module's own role or package, which is not what REPO names."""
    module = tmp_path / "scripts" / "x" / "tests" / "test_y.py"
    assert (
        local_repo_roots(
            "HERE = Path(__file__).resolve().parents[1]\n", module, repo=tmp_path
        )
        == []
    )


def test_every_grandfathered_reader_still_reads_one():
    """A converted module must leave the list, which is what keeps it shrinking.

    Without this, an entry outlives the read it was written for and silently re-permits the
    whole module — the same way a stale exemption does above.
    """
    clean = sorted(
        name
        for name in SHELL_SOURCE_READERS
        if (REPO / name).exists()
        and not shell_template_source_reads((REPO / name).read_text())
    )
    assert clean == [], (
        "these modules no longer read a shell template's source — drop them from "
        f"SHELL_SOURCE_READERS: {clean}"
    )


def test_no_new_test_module_reads_a_shell_templates_source():
    offenders = {
        _rel(p): reads
        for p in _test_modules()
        if _rel(p) not in SHELL_SOURCE_READERS
        and (reads := shell_template_source_reads(p.read_text()))
    }
    assert offenders == {}, (
        "these tests assert on a *.sh.j2's SOURCE, so a value moved into a role default leaves "
        "the assertion matching `{{ ... }}`. Use `_shell_render.rendered_shell_text(plane, "
        "role, name)`, or `render_shell_script` where the test runs the script: "
        f"{offenders}"
    )


def test_a_shell_template_source_read_is_flagged_in_each_spelling():
    for source in (
        'DRILL = K3S / "templates" / "drill.sh.j2"\nDRILL.read_text()\n',
        'T: Path = Path("ansible/roles/setup/k3s/templates/drill.sh.j2")\nT.read_bytes()\n',
        '(ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2").read_text()\n',
        'for p in TEMPLATES.glob("*.sh.j2"):\n    pass\n',
    ):
        assert shell_template_source_reads(source) != [], source


def test_a_resolver_fed_source_read_is_flagged_in_each_spelling():
    """A module that gets its paths from a resolver names no `*.sh.j2` anywhere (#3200)."""
    for source in (
        "for tpl, _task_file, _cron, _env in ct.iter_cron_targets():\n"
        "    tpl.read_text()\n",
        "for tpl in discover_templates():\n    tpl.read_text()\n",
        "CRONS = ct.cron_job_scripts()\nfor tpl in CRONS:\n    tpl.read_text()\n",
        "CRONS = ct.cron_job_scripts()\n"
        "for tpl, _task in CRONS.items():\n"
        "    tpl.read_bytes()\n",
    ):
        assert shell_template_source_reads(source) != [], source


def test_a_resolver_fed_fixture_parameter_is_flagged_in_each_spelling():
    """A fixture hands its resolver result to consumers as a PARAMETER (#3206).

    The name the fixture is declared under is the name each consumer takes it by, so the
    detector resolves the fixture's return expression once and binds that name.
    """
    for source in (
        "@pytest.fixture(scope='module')\n"
        "def cron_map():\n"
        "    return ct.cron_job_scripts()\n"
        "def test_x(cron_map):\n"
        "    for tpl in cron_map:\n"
        "        tpl.read_text()\n",
        "@pytest.fixture\n"
        "def cron_map():\n"
        "    yield ct.cron_job_scripts()\n"
        "def test_x(cron_map):\n"
        "    for tpl, _task in cron_map.items():\n"
        "        tpl.read_bytes()\n",
    ):
        assert shell_template_source_reads(source) != [], source


def test_a_fixture_returning_something_else_does_not_bind_its_name():
    """Only a resolver call binds — a fixture over a synthetic tree or a roster does not."""
    for source in (
        "@pytest.fixture\n"
        "def cron_map(tmp_path):\n"
        "    return ct.cron_job_scripts(tmp_path)\n"
        "def test_x(cron_map):\n"
        "    for tpl in cron_map:\n"
        "        tpl.read_text()\n",
        "@pytest.fixture\n"
        "def names():\n"
        "    return frozenset({'drill.sh.j2'})\n"
        "def test_x(names):\n"
        "    for tpl in names:\n"
        "        tpl.read_text()\n",
    ):
        assert shell_template_source_reads(source) == [], source


def test_a_source_read_through_a_local_helper_is_flagged_in_each_spelling():
    """The helper's parameter shadows the name the caller passed, so the read it performs is
    invisible and the call site names no template. The CALL is the read instead (#3220)."""
    helper = (
        "def _source(path):\n"
        "    return '\\n'.join(l for l in path.read_text().splitlines())\n"
    )
    for call in (
        'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n_source(CHECK)\n',
        'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n_source(path=CHECK)\n',
        '_source(ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2")\n',
    ):
        assert shell_template_source_reads(helper + call) != [], call


def test_a_helper_that_does_not_read_its_parameter_is_not_flagged():
    """Four ways a call is not a read: the helper reads nothing, the helper is IMPORTED, a
    parameter of the calling test shadows the helper's name, and the argument is a `tmp_path`
    fixture.

    The imported case is why only a locally defined function qualifies — resolving one would
    flag every `render_shell_script("setup", "k3s", "drill.sh.j2")` call, whose `name` argument
    ends in `.sh.j2`.
    """
    bound = 'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n'
    for source in (
        "def _name(path):\n    return path.name\n" + bound + "_name(CHECK)\n",
        "from _shell_render import render_shell_script\n"
        'render_shell_script("setup", "k3s", "drill.sh.j2")\n',
        "def _source(path):\n    return path.read_text()\n"
        + bound
        + "def test_x(_source):\n    _source(CHECK)\n",
        # A template the module wrote itself is its own fixture, however it is read.
        "def _source(path):\n    return path.read_text()\n"
        '_source(tmp_path / "good.sh.j2")\n',
    ):
        assert shell_template_source_reads(source) == [], source


def test_a_resolver_pointed_at_a_synthetic_tree_is_not_flagged():
    """`cron_job_scripts(tmp_path)` resolves the role tree the test wrote itself."""
    assert (
        shell_template_source_reads(
            "for tpl in ct.cron_job_scripts(tmp_path):\n    tpl.read_text()\n"
        )
        == []
    )


def test_a_resolver_fed_loop_binds_only_the_template():
    """`iter_cron_targets` yields the template first; reading its task FILE is not the rule."""
    assert (
        shell_template_source_reads(
            "for _tpl, task_file, _cron, _env in ct.iter_cron_targets():\n"
            "    task_file.read_text()\n"
        )
        == []
    )


def test_a_parameter_shadowing_a_resolver_name_is_not_flagged():
    """`_shell_render.rendered_or_source_text(path)` reads a parameter, not the loop's path."""
    assert (
        shell_template_source_reads(
            "for path in discover_templates():\n    render_template(path)\n"
            "def rendered_or_source_text(path):\n    return path.read_text()\n"
        )
        == []
    )


def test_a_render_and_a_bare_name_are_not_flagged():
    """The sanctioned call takes a (plane, role, name) TRIPLE, so no path is built at all."""
    assert (
        shell_template_source_reads(
            "from _shell_render import rendered_shell_text\n"
            'SCRIPT = rendered_shell_text("setup", "k3s", "drill.sh.j2")\n'
        )
        == []
    )
    # A roster of names a census must find, and a synthetic template the module wrote itself.
    assert shell_template_source_reads('NAMES = frozenset({"drill.sh.j2"})\n') == []
    assert (
        shell_template_source_reads(
            'tpl = tmp_path / "good.sh.j2"\ntpl.write_text("")\ntpl.read_text()\n'
        )
        == []
    )
