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

from lib.script_classify import SUFFIXES, by_name, file_text

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


def indirect_test(name: str, test_files: list[Path], scripts: Path) -> tuple[str, str]:
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
    """
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
    ordered = sorted(test_files, key=lambda p: home is None or home not in p.parents)
    for path in ordered:
        text = file_text(path)
        if import_re and import_re.search(text):
            return path.name, "import"
    for path in ordered:
        if path_re.search(file_text(path)) and not _is_another_scripts_test(
            path, scripts
        ):
            return path.name, "path"
    return "", ""


def _is_another_scripts_test(test: Path, scripts: Path) -> bool:
    """Is this test named after some script in `scripts/`, rather than about the tree?"""
    subject = test.name[len("test_") :]
    known = by_name(scripts)
    return any(f"{Path(subject).stem}{suffix}" in known for suffix in SUFFIXES)
