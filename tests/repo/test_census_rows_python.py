"""Textual censuses of the tracked Python, one `_row_table.Census` row each.

Each row replaces a test file of its own. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. The harness supplies the
`subject gone` failure, the floor, the named members, the stale-allow check and the red/green
proof (`tests/_row_table.py`).

The rows that police the suite's own guards are in `test_census_rows_suite.py`, and the
docs, template and workflow rows in `test_census_rows_text.py`.

Run: uv run pytest tests/repo/test_census_rows_python.py
"""

import ast
import re
import tomllib
from functools import cache
from pathlib import PurePosixPath

import pytest
from _helpers import REPO
from _condition_status_row import CONDITION_STATUS_IS_PARSED
from _future_annotations_row import NO_FUTURE_ANNOTATIONS
from _printed_json_row import PRINTED_JSON_IS_COMPACT
from _rendered_documents_row import RENDERED_OBJECTS_ARE_PARSED
from _row_table import (
    Census,
    Subject,
    check,
    lines_matching,
    proof_problems,
    text_lacks,
    tracked,
)

SELF = "tests/repo/test_census_rows_python.py"


def _parse(subject: Subject) -> ast.AST | None:
    """The module's AST, or None for a file this interpreter cannot parse."""
    try:
        return ast.parse(subject.text)
    except SyntaxError:
        return None


# ── "<host>" in x ─────────────────────────────────────────────────────────────────────

# Close to what CodeQL matches, not to what a valid hostname is.
HOST_SHAPED = re.compile(
    r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.(io|com|net|org|local|dev|lan)$"
)


def _host_membership_offence(subject: Subject) -> list[str]:
    tree = _parse(subject)
    return [
        f"line {node.lineno}"
        for node in (ast.walk(tree) if tree else ())
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and HOST_SHAPED.match(node.left.value)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    ]


# ── yaml.safe_load ────────────────────────────────────────────────────────────────────

_BANNED_YAML = frozenset({"safe_load", "safe_load_all"})

# Modules the census must find parsing YAML through the helper.
YAML_FAST_USERS = frozenset(
    {
        "tests/_k8s_render.py",
        "scripts/lib/invocation_sites.py",
        "scripts/validate/render_lib/compose.py",
    }
)


def _pyyaml_offence(subject: Subject) -> list[str]:
    """A CALL to `yaml.safe_load[_all]`; a docstring mentioning one is not a call."""
    # The attribute is an identifier, so a file that never spells it calls neither.
    if "safe_load" not in subject.text:
        return []
    tree = _parse(subject)
    return [
        f"line {node.lineno}: yaml.{node.func.attr}"
        for node in (ast.walk(tree) if tree else ())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _BANNED_YAML
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
    ]


# ── Path(__file__).resolve().parents[N] ───────────────────────────────────────────────

# `parents[0]` is the file's own directory, a local fact, so the pattern starts at 1.
_ANCHOR = re.compile(
    r"(?:_?Path|pathlib\.Path)\(__file__\)\.resolve\(\)(?:\.parents\[[1-9]\d*\]|(?:\.parent){2,})"
)


# ── subprocess.run(["git", ...]) or subprocess.Popen(["git", ...]) under scripts/ ─────

_ROUTED = frozenset({"git", "gh", "kubectl"})
# A long-lived `git cat-file --batch` needs the same GIT_*-free environment as a one-shot call;
# `lib.git.git_process` is its routed form.
_RAW_CALLS = frozenset({"run", "Popen"})


def _scripts_production_modules() -> list[str]:
    """Every scripts/ module but tests and lib/; lib/facts/ is a client of lib.git, so it counts."""
    return [
        rel
        for rel in tracked("scripts/*.py")
        if "/tests/" not in rel
        and (not rel.startswith("scripts/lib/") or rel.startswith("scripts/lib/facts/"))
    ]


def _raw_call_offence(subject: Subject) -> list[str]:
    tree = _parse(subject)
    hits = []
    for node in ast.walk(tree) if tree else ():
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _RAW_CALLS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
            and isinstance(node.args[0], ast.List)
            and node.args[0].elts
        ):
            continue
        first = node.args[0].elts[0]
        if isinstance(first, ast.Constant) and first.value in _ROUTED:
            hits.append(
                f"line {node.lineno}: raw subprocess.{node.func.attr} of {first.value}"
            )
    return hits


# ── Top-level module basenames across import roots ───────────────────────────────────


def pythonpath_roots() -> list[str]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return list(cfg["tool"]["pytest"]["ini_options"]["pythonpath"])


@cache
def _import_roots() -> frozenset[str]:
    """The `pythonpath` roots, every role's `files/`, and every `scripts/<dir>` with tests.

    A role test reaches its own `files/` through a `sys.path.insert` at collection time, and a
    test under a `scripts/<dir>` off `pythonpath` may insert `<dir>` itself
    (`.claude/rules/python-layout.md`). Every such insert lands in the one session a run
    shares, so each is an import root as much as a `pythonpath` entry is.
    """
    roles = {
        rel.rsplit("/", 1)[0]
        for rel in tracked("k8s/roles/*/files/*.py")
        if len(parts := rel.split("/")) == 5 and parts[3] == "files"
    }
    tested = {rel.split("/tests/")[0] for rel in tracked("scripts/*/tests/*.py")}
    return frozenset(pythonpath_roots()) | roles | tested


def _top_level_modules() -> list[str]:
    """Every bare-importable top-level module: a `*.py` directly in an import root.

    A module in a subdirectory of a root is reached as `package.module` and shadows nothing.
    `conftest.py` is skipped: pytest imports each under its own unique module key.
    """
    return [
        rel
        for rel in tracked("*.py")
        if str(PurePosixPath(rel).parent) in _import_roots()
        and not rel.endswith("/conftest.py")
    ]


@cache
def _homes_by_basename() -> dict[str, tuple[str, ...]]:
    homes: dict[str, list[str]] = {}
    for rel in _top_level_modules():
        homes.setdefault(PurePosixPath(rel).stem, []).append(rel)
    return {name: tuple(paths) for name, paths in homes.items()}


def _shadowing_offence(subject: Subject) -> list[str]:
    """The other import roots holding a module of this one's name."""
    others = [
        rel
        for rel in _homes_by_basename().get(PurePosixPath(subject.rel).stem, ())
        if rel != subject.rel
    ]
    return [f"also a top-level module at {rel}" for rel in others]


ROWS = (
    NO_FUTURE_ANNOTATIONS,
    Census(
        name="no-host-shaped-membership-literal",
        reason=(
            'CodeQL\'s py/incomplete-url-substring-sanitization fires on `"<host>.<tld>" in x` '
            "and cannot tell a substring check on a URL from exact membership in a collection, "
            'which is what this repo writes. On a collection write `any(x == "<host>" for x in '
            "seq)`; on a URL, parse it and compare the host. On rendered text `in` is right: "
            "match a surrounding anchor with a regex, or add the file to `allow` with the reason."
        ),
        files=lambda: tracked("*.py"),
        offence=_host_membership_offence,
        red=(
            Subject("a.py", 'assert "backups.longhorn.io" in calls\n'),
            Subject("b.py", 'assert "www.local.example.com" not in fqdns\n'),
        ),
        green=(
            Subject("a.py", 'assert any(c == "backups.longhorn.io" for c in calls)\n'),
            Subject("b.py", 'assert "delete" in argv\n'),
            Subject("c.py", 'assert host in {"a.longhorn.io"}\n'),
        ),
        min_matches=100,
        must_find=frozenset({"tests/_helpers.py"}),
    ),
    Census(
        name="yaml-parses-through-libyaml",
        reason=(
            "`lib.yaml_fast` parses the same YAML 1.1 safe schema through libyaml, 0.42s against "
            "0.04s over the 184 task files. A new `yaml.safe_load` call parses correctly and "
            "silently gives back part of that saving. Use `from lib import yaml_fast`; a call "
            "that must use PyYAML's own parser goes in `allow` with the reason. Role `files/` "
            "are out of scope: they cannot reach `scripts/lib` and run cluster-side."
        ),
        files=lambda: tracked("scripts/*.py", "tests/*.py", "hosts/tests/*.py"),
        offence=_pyyaml_offence,
        red=(
            Subject("a.py", "import yaml\ndata = yaml.safe_load(text)\n"),
            Subject("b.py", "import yaml\ndocs = list(yaml.safe_load_all(text))\n"),
        ),
        green=(
            Subject("a.py", '"""Role defaults are read with yaml.safe_load."""\n'),
            Subject(
                "b.py", "import yaml\nyaml.load(stream, Loader=yaml.CSafeLoader)\n"
            ),
            Subject("c.py", "safe_load = other.safe_load\nsafe_load(text)\n"),
        ),
        min_matches=200,
        must_find=YAML_FAST_USERS,
        allow={
            "scripts/lib/tests/test_yaml_fast.py": (
                "asserts the swap's equivalence by comparing the two parsers"
            ),
            "scripts/diagnostics/probe_lib/health_docker.py": (
                "one function-local call on a path that runs once"
            ),
            "hosts/tests/test_host_python_invocations.py": (
                "one function-local call on a path that runs once"
            ),
            "hosts/tests/test_github_ruleset_drift.py": (
                "one function-local call on a path that runs once"
            ),
        },
    ),
    Census(
        name="yaml-fast-users-still-use-it",
        reason=(
            "The positive half of yaml-parses-through-libyaml: these modules must still parse "
            "through the helper, or the census above could pass with the helper unused."
        ),
        files=lambda: sorted(YAML_FAST_USERS),
        offence=text_lacks("yaml_fast", "parses YAML through yaml_fast"),
        red=(Subject("a.py", "import yaml\n"),),
        green=(Subject("a.py", "from lib import yaml_fast\n"),),
        must_find=YAML_FAST_USERS,
    ),
    Census(
        name="path-anchors-come-from-helpers",
        reason=(
            "A `Path(__file__).resolve().parents[N]` index is position-dependent and fails "
            "silently: from one directory deeper `parents[2]` is `ansible/`, a real directory, "
            "and a guard globbing under it passes on nothing. Import REPO, ANSIBLE or a role "
            "tree from `_helpers` instead."
        ),
        files=lambda: tracked("tests/*.py", "hosts/tests/*.py"),
        offence=lines_matching(_ANCHOR),
        red=(
            Subject("a.py", "ROLE = Path(__file__).resolve().parents[2] / 'ansible'\n"),
            Subject("b.py", "_REPO = pathlib.Path(__file__).resolve().parents[2]\n"),
            Subject("c.py", "_REPO = _Path(__file__).resolve().parents[2]\n"),
            Subject("d.py", "REPO = Path(__file__).resolve().parent.parent\n"),
        ),
        green=(
            Subject("a.py", "from _helpers import REPO\n"),
            Subject("b.py", "HERE = Path(__file__).resolve().parents[0]\n"),
            Subject("c.py", "HERE = Path(__file__).resolve().parent\n"),
        ),
        min_matches=100,
        must_find=frozenset({"tests/repo/test_role_claude_md.py"}),
        allow={SELF: "the red fixtures above hold the offending spelling as text"},
    ),
    PRINTED_JSON_IS_COMPACT,
    Census(
        name="no-two-import-roots-share-a-module-basename",
        reason=(
            "There are no `__init__.py` files, so every `*.py` directly in an import root is a "
            "bare top-level module, and two roots holding one name resolve `import <name>` by "
            "`sys.path` order rather than by construction. Two roles' `files/app.py` failed six "
            "tests only in a full run (#2608), and a validator in `scripts/validate` hid a filter "
            "plugin from `pytest scripts` (#5956). Rename one. "
            "`scripts/docs/tests/test_gen_reference_scripts.py` holds a different invariant, "
            "basenames at ANY depth under `scripts/`, for the reference page."
        ),
        files=_top_level_modules,
        offence=_shadowing_offence,
        # The live index decides, so the red subject is a second home for a real module.
        red=(
            Subject("scripts/lib/registry.py", ""),
            Subject("hosts/lib/render_templates.py", ""),
        ),
        green=(Subject("scripts/lib/no_other_root_holds_this_name.py", ""),),
        min_matches=100,
        must_find=frozenset(
            {
                "hosts/lib/host_lib.py",
                "k8s/roles/monitor-bridge/files/registry.py",
                "k8s/roles/homelab-mcp/files/safe_reads.py",
                "scripts/lib/cli_registry.py",
                "scripts/lib/repo_paths.py",
                "scripts/validate/render_templates.py",
                "scripts/diagnostics/probe.py",
            }
        ),
    ),
    Census(
        name="git-and-gh-go-through-lib",
        reason=(
            "`lib.git.git` strips every GIT_* variable so `cwd` alone picks the repository, and "
            "`lib.gh.gh` disables the prompt and update notifier so a cron cannot hang. A raw "
            '`subprocess.run(["git", ...])` has neither, and the repo re-grew fourteen. A raw '
            "`subprocess.Popen` of git lacks the scrub too; `lib.git.git_process` starts a "
            "long-lived one. Role `files/*.py` stay raw on purpose: they deploy to hosts "
            "without `scripts/lib`."
        ),
        files=_scripts_production_modules,
        offence=_raw_call_offence,
        red=(
            Subject("a.py", 'subprocess.run(\n    ["git", "status"],\n)'),
            Subject("b.py", 'subprocess.run(["gh", *args], check=True)'),
            Subject(
                "c.py",
                'subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE)',
            ),
        ),
        green=(
            Subject("a.py", 'git("status", cwd=repo)'),
            Subject("e.py", 'subprocess.Popen(["tee", capture], stdin=read_end)'),
            Subject("b.py", 'subprocess.run(["ssh", host, "git rev-parse HEAD"])'),
            Subject(
                "c.py", 'subprocess.CompletedProcess(args=["gh", *args], returncode=1)'
            ),
            Subject(
                "d.py",
                '"""A raw `subprocess.run(["git", ...])` here read the wrong repo."""',
            ),
        ),
        must_find=frozenset(
            f"scripts/{rel}"
            for rel in (
                "dev/fanout_lib/place.py",
                "dev/pytest_shard.py",
                "dev/fanout_lib/clean.py",
                "dev/fanout_lib/transport.py",
                "dev/fanout_lib/signing.py",
                "validate/root_ignored_files.py",
                "infra_map/live.py",
                "lib/facts/citations.py",
                "lib/facts/lint.py",
                "lib/facts/notice.py",
            )
        ),
    ),
    Census(
        name="host-check-textfiles-go-through-host-lib",
        reason=(
            "node-exporter merges a `homelab_check_*` family across every textfile and drops "
            "each series whose file disagrees on HELP or TYPE, while the file's mtime still "
            "reaches Prometheus. A host script that writes the four series itself loses all "
            "four, and its NotReporting rule pages from the first run. Call "
            "`host_lib.write_check_textfile`, which carries homelab-check-lib.sh's HELP and "
            "TYPE lines."
        ),
        files=lambda: [
            rel
            for rel in tracked("hosts/*.py")
            if "/tests/" not in rel and rel != "hosts/lib/host_lib.py"
        ],
        offence=lines_matching(re.compile(r"""["']homelab_check_\w+""")),
        red=(
            Subject("a.py", '("homelab_check_success", int(ok)),\n'),
            Subject("b.py", "f'homelab_check_success{{check=\"{CHECK}\"}} 1'\n"),
        ),
        green=(
            Subject("a.py", "write_check_textfile(path, CHECK, success=ok, **kw)\n"),
            Subject("b.py", '"""Exits 0 with `homelab_check_success` 0."""\n'),
        ),
        min_matches=40,
        must_find=frozenset(
            {
                "hosts/lib/checkout_sync.py",
                "hosts/components/claude-code/files/claude_guardrail_ack.py",
                "hosts/components/claude-code/files/claude_dotfiles_mirror.py",
                "hosts/components/claude-code/files/claude_weekly_summary.py",
            }
        ),
    ),
    CONDITION_STATUS_IS_PARSED,
    RENDERED_OBJECTS_ARE_PARSED,
)
_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)


def test_every_pythonpath_root_is_a_real_directory():
    """A root that moved would leave the basename census silently short."""
    missing = [root for root in pythonpath_roots() if not (REPO / root).is_dir()]
    assert not missing, (
        f"pyproject pythonpath names directories that do not exist: {missing}"
    )


def test_the_future_annotations_exemption_covers_only_the_pod_image_modules():
    """An exemption that matched nothing would gate pod scripts on a rule their image's python cannot take."""
    exempt = set(tracked("*.py")) - set(NO_FUTURE_ANNOTATIONS.files())
    assert "k8s/roles/qbittorrent/files/apply_prefs.py" in exempt
    assert not sorted(rel for rel in exempt if not rel.startswith("k8s/roles/"))
