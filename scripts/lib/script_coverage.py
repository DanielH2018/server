#!/usr/bin/env python3
"""Which test, if any, exercises a given script — the reference page's Tests column.

Split out of ``scripts/docs/reference/scripts.py`` on 2026-09-04. The direct case
(``test_<name>.py`` beside the module or in its ``tests/`` sibling) stays in the generator,
because it is a two-line file check; what lives here is the indirect case, which is where the
judgement calls are and where every past miscredit came from.

The census helpers come from ``lib.script_classify`` rather than from the generator: a leaf
never imports the facade it was split out of, and both modules need the same "which files are
scripts" answer.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

from lib.script_classify import SUFFIXES, by_name, file_text, importers

__all__ = [
    "candidate_test_files",
    "indirect_test",
]


def candidate_test_files(repo: Path, scripts: Path) -> list[Path]:
    """Every pytest file that could be about a script, in any of the test roots.

    A subdirectory's tests live either beside its modules (`scripts/<dir>/test_*.py`, not
    yet split) or in its own `tests/` sibling (`scripts/<dir>/tests/test_*.py`, the split
    layout) — both are covered so a mid-migration tree and a finished one both scan clean.
    """
    return (
        sorted(scripts.glob("test_*.py"))
        + sorted(scripts.glob("*/test_*.py"))
        + sorted(scripts.glob("*/tests/test_*.py"))
        + sorted((repo / "ansible" / "tests").rglob("test_*.py"))
    )


def indirect_test(
    name: str,
    test_files: list[Path],
    scripts: Path,
    imports: dict[str, set[str]] | None = None,
) -> tuple[str, str]:
    """The test that names this script but is not called `test_<name>.py`.

    `gitops_tick.sh` has five tests, in `ansible/tests/deploy/test_gitops_manual_trigger.py`, and
    reporting it as untested on the naming convention alone put a covered script on the gap
    list. A module with no test file of its own is likewise often reached through the entry
    point that imports it.

    The two mechanisms are not equally trustworthy, so they are not treated alike.

    An import is real exercise, and is credited wherever it appears. A path string is only a
    mention, and a mention inside `test_<other script>.py` is that other script's test
    talking about this one — `test_deploy_detach_notify.py` opens "the `scripts/deploy.sh`
    --detach completion notifier", which credited 15 KB of shell that runs on every deploy to
    a test of the notifier. Matching the bare stem credited `deploy.sh` to a test that merely
    says "deploy", and credited three scripts to THIS generator's own test, which names every
    script in the tree by construction.

    `imports` is the import graph from `script_classify.importers`. The caller passes the one
    it already built, because building it parses every Python file in the tree and this
    function runs once per script — computing it here made a docs refresh quadratic.
    """
    imports = importers(scripts) if imports is None else imports
    callers = _non_test_importers(name, scripts, imports)
    stem = re.escape(Path(name).stem)
    path_re = re.compile(rf"scripts/(?:[A-Za-z0-9_]+/)?{re.escape(name)}")
    if not name.endswith(".py"):
        import_re = None
    elif Path(name).stem.isidentifier():
        # Two spellings reach the same module. `import live` and `from live import x` name
        # it as a top-level module; `from infra_map import live` names it as a member of its
        # package, which is how a module in a subdirectory that shares its basename with
        # another one has to be imported. Matching only the first reported infra_map's `live`
        # and `render` as untested the moment they took the package form.
        import_re = re.compile(
            rf"^\s*(?:from|import)\s+{stem}\b"
            rf"|^\s*from\s+[\w.]+\s+import\s+.*\b{stem}\b",
            re.MULTILINE,
        )
    else:
        # A hyphenated filename is not a valid module name, so NO import statement can
        # name it — `glenstone-bot.py` is loadable only via spec_from_file_location. Its
        # test quotes the filename, which is a load and not a mention, so it counts as an
        # import. Requiring a bare `import` here reported two genuinely tested bots as
        # untested, which is the page telling a story about coverage that isn't true.
        import_re = re.compile(rf"""['"]{re.escape(name)}['"]""")

    # A test living beside the module wins over one that merely imports it from elsewhere.
    # Several tests may import the same module, and taking whichever sorted first credited
    # `validate/k8s_manifests.py` to `test_probe_health.py` — a caller's test, naming the
    # wrong suite on a page whose whole job is to say where a script's coverage lives.
    module = by_name(scripts).get(name)
    home = module.parent if module else None
    ordered = sorted(
        test_files,
        key=lambda p: (
            home is None or home not in p.parents,
            _name_rank(p, name, callers),
        ),
    )
    for path in ordered:
        text = file_text(path)
        if import_re and import_re.search(text):
            return path.name, "import"
    for path in ordered:
        if path_re.search(file_text(path)) and not _is_another_scripts_test(
            path, scripts
        ):
            return path.name, "path"
    inherited = _importers_test(name, scripts, callers)
    if inherited:
        return inherited, "importer"
    return "", ""


def _name_rank(test: Path, name: str, callers: list[Path]) -> int:
    """How well a test's own name says it is about `name`. Lower wins.

    Locality decides first, and several suites in one directory import the same module, so
    something has to break the tie. Leaving it to the order `candidate_test_files` happened
    to build credited `validate/shell_templates.py` to `test_backup_health_shim.py` — a suite
    about one rendered shim — when `test_validate_shell_templates.py` is its canonical suite.
    A module cannot always claim `test_<stem>.py`: pytest names modules by basename repo-wide,
    so `shell_templates.py` is deliberately tested by `test_validate_shell_templates.py`
    (`suite_for()` in `validate/tests/test_every_validator_has_a_red_proof.py` states that
    convention). Carrying the stem anywhere in the name is therefore the signal.

    A leaf with exactly ONE non-test importer takes its importer's name as a second signal:
    `lib/shell_lint.py` is imported only by `shell_templates.py`, and the suite named after
    that facade is where its coverage lives. The single-importer gate is what keeps this from
    reshuffling `repo_paths.py`, which 45 scripts import and no one suite is canonical for.
    """
    if _carries(test, Path(name).stem):
        return 0
    if len(callers) == 1 and _carries(test, callers[0].stem):
        return 1
    return 2


def _carries(test: Path, stem: str) -> bool:
    """Does the test's filename name `stem` as a whole word?

    A bare substring is too loose to decide a tie: `lib/gh.py` would claim every suite with
    `gh` anywhere in its name. Test names are underscore-delimited, so the whole-word form
    still matches `test_validate_shell_templates.py` for `shell_templates`.
    """
    return f"_{stem}_" in f"{test.stem}_"


def _non_test_importers(
    name: str, scripts: Path, imports: dict[str, set[str]]
) -> list[Path]:
    """The scripts that import `name`, excluding anything living under a `tests/` directory.

    A fixture module such as `dev/tests/_findings_fakes.py` imports the leaf to fake it. It
    is not a caller whose suite could stand in for the leaf's own, so it is not one here.
    """
    known = by_name(scripts)
    found = imports.get(Path(name).stem, set())
    paths = [known[caller] for caller in found if caller in known]
    return sorted(p for p in paths if "tests" not in p.parts)


def _importers_test(name: str, scripts: Path, callers: list[Path]) -> str:
    """The direct test of the script that imports this one, or "".

    A leaf split out of a larger module is exercised by its parent's suite, which neither is
    called `test_<leaf>.py` nor names the leaf anywhere — so the page called `findings_gh.py`,
    `findings_model.py` and `findings_plans.py` untested the day `findings.py` was split into
    them. That reads as coverage lost, when what happened is that a tested module was divided
    up. Crediting the importer's suite says what is true: a test does exercise this code, and
    it is the importer's.

    Only a DIRECT `test_<importer>.py` counts, and only one hop. Chaining through an importer
    that is itself credited indirectly would walk the import graph, where a cycle and a
    50-importer library both wait; one hop cannot loop and cannot travel.
    """
    module = by_name(scripts).get(name)
    home = module.parent if module else None
    ordered = sorted(callers, key=lambda p: (home is None or p.parent != home, p.name))
    for caller in ordered:
        for direct in (
            caller.parent / "tests" / f"test_{caller.stem}.py",
            caller.parent / f"test_{caller.stem}.py",
        ):
            if direct.is_file():
                return direct.name
    return ""


def _is_another_scripts_test(test: Path, scripts: Path) -> bool:
    """Is this test named after some script in `scripts/`, rather than about the tree?"""
    subject = test.name[len("test_") :]
    known = by_name(scripts)
    return any(f"{Path(subject).stem}{suffix}" in known for suffix in SUFFIXES)
