"""Tests for how `lib/script_coverage.indirect_test` attributes a suite to a script.

The reference page's *Tests* column is only as good as this attribution, so the cases
that decide a tie or inherit a credit live here rather than in the generator's own suite,
which is capped by the module-length ratchet.

Fixture-driven: a synthetic scripts/ directory under tmp_path.
Run: uv run pytest scripts/lib/tests/test_script_coverage.py
"""

import textwrap
from pathlib import Path

from docs.reference import scripts as g
from lib import script_classify as sc, script_coverage as cov


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def _repo(tmp_path):
    """A bare repo root and its scripts/ dir.

    These cases assert on the *Tests* column alone, so none of the invocation sites the
    generator's own fixture builds for the *Reached by* column are needed here.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    return tmp_path, scripts


def test_the_suite_carrying_the_modules_stem_wins_a_local_tie(tmp_path):
    """Locality alone left the winner to alphabetical order (issue #1133).

    Three suites in `scripts/validate/tests/` import `shell_templates`, so the local-first
    sort settled nothing and `test_backup_health_shim.py` -- a suite about one rendered
    shim -- was credited, on a page whose whole job is to say where coverage lives.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "subject.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "tests" / "test_aaa_shim.py", "from pkg import subject\n")
    _write(
        scripts / "pkg" / "tests" / "test_validate_subject.py",
        "from pkg import subject\n",
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["subject.py"]["indirect_tests"] == "test_validate_subject.py"


def test_the_local_tie_falls_back_when_no_suite_carries_the_stem(tmp_path):
    """The RED half: the stem is a tie-break, not a requirement.

    Requiring it would drop every module whose covering suite is named for something else
    back to untested -- the understatement the tie-break exists to fix.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "orphan.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "tests" / "test_aaa_shim.py", "from pkg import orphan\n")
    _write(scripts / "pkg" / "tests" / "test_zzz_other.py", "from pkg import orphan\n")
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["orphan.py"]["indirect_tests"] == "test_aaa_shim.py"


def test_a_stem_matches_as_a_whole_word_not_a_substring():
    """`lib/gh.py` must not claim every suite with `gh` anywhere in its name.

    Test names are underscore-delimited, so the whole-word form still reaches
    `test_validate_shell_templates.py` for `shell_templates`.
    """
    assert cov._carries(Path("test_validate_shell_templates.py"), "shell_templates")
    assert not cov._carries(Path("test_highlight_parser.py"), "gh")


def test_a_single_importers_suite_breaks_a_tie_the_stem_cannot(tmp_path):
    """`lib/shell_lint.py` is imported only by `shell_templates.py`.

    No suite carries `shell_lint`, so without this signal the credit fell to whichever
    sibling sorted first. The facade's own suite is where the leaf's coverage lives.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "leaf.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "facade.py", '"""Summary."""\nfrom pkg import leaf\n')
    _write(scripts / "pkg" / "tests" / "test_aaa_other.py", "from pkg import leaf\n")
    _write(scripts / "pkg" / "tests" / "test_facade.py", "from pkg import leaf\n")
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_tests"] == "test_facade.py"


def test_two_importers_get_no_importer_tie_break(tmp_path):
    """The RED half: the gate is exactly ONE non-test importer.

    `repo_paths.py` has 45 importers and no canonical suite among them; letting any of
    their names win would reshuffle its row for no reason.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "shared.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "facade.py", '"""Summary."""\nfrom pkg import shared\n')
    _write(scripts / "pkg" / "second.py", '"""Summary."""\nfrom pkg import shared\n')
    _write(scripts / "pkg" / "tests" / "test_aaa_other.py", "from pkg import shared\n")
    _write(scripts / "pkg" / "tests" / "test_facade.py", "from pkg import shared\n")
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["shared.py"]["indirect_tests"] == "test_aaa_other.py"


def test_a_split_out_leaf_inherits_its_importers_test(tmp_path):
    """Splitting a module well must not read as coverage lost (issue #1136).

    A leaf extracted from a tested module is exercised by its parent's suite, which neither
    is called `test_<leaf>.py` nor names the leaf anywhere -- so `findings.py`'s split
    reported three new untested scripts the day it landed.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "leaf.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "parent.py", '"""Summary."""\nfrom pkg import leaf\n')
    _write(scripts / "pkg" / "tests" / "test_parent.py", "from pkg import parent\n")
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_tests"] == "test_parent.py"
    assert rows["leaf.py"]["indirect_via"] == "importer"


def test_an_importer_with_no_test_of_its_own_credits_nothing(tmp_path):
    """The RED half: one hop, and only to a DIRECT `test_<importer>.py`.

    Chaining through an importer that is itself credited indirectly would walk the import
    graph, where a cycle and a 50-importer library both wait.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "leaf.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "parent.py", '"""Summary."""\nfrom pkg import leaf\n')
    _write(scripts / "pkg" / "grandparent.py", '"""x"""\nfrom pkg import parent\n')
    _write(
        scripts / "pkg" / "tests" / "test_grandparent.py",
        "from pkg import grandparent\n",
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_tests"] == ""
    assert rows["leaf.py"]["indirect_via"] == ""


def test_a_test_only_importer_cannot_stand_in_for_the_leafs_own_suite(tmp_path):
    """A fixture module such as `dev/tests/_findings_fakes.py` imports the leaf to fake it.

    It is not a caller whose suite could cover the leaf, so it must not supply the credit.
    """
    _, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "leaf.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "tests" / "_fakes.py", "from pkg import leaf\n")
    imports = sc.importers(scripts)
    assert cov._non_test_importers("leaf.py", scripts, imports) == []


def test_the_split_findings_leaves_are_not_reported_untested(live_script_rows):
    """Non-vacuity against the real tree for the class this rule was written for.

    The fixtures above prove the rule fires on inputs handed to it; this proves it still
    finds the class it was written for. Named members, so a failure says which leaf moved.

    The via was `importer` until the dotted-path branch landed: `test_findings.py` says
    `from dev.findings_gh import run`, which is the leaf imported directly and so a
    stronger credit than inheriting the facade's suite. The suite named is the same either
    way. `test_the_infra_map_facade_members_inherit_the_facades_suite` below is what keeps
    the `importer` via itself non-vacuous.
    """
    rows = {r["name"]: r for r in live_script_rows}
    for name in ("findings_gh.py", "findings_model.py", "findings_plans.py"):
        assert rows[name]["indirect_via"] == "import", name
        assert rows[name]["indirect_tests"] == "test_findings.py", name


def test_the_shell_template_validators_are_credited_to_their_canonical_suite(
    live_script_rows,
):
    """Non-vacuity for the stem tie-break, against the real tree.

    Both were credited to `test_backup_health_shim.py`, a suite about one rendered shim.
    `shell_templates.py` can never win a DIRECT match: pytest names modules by basename
    repo-wide, so it is deliberately `test_validate_shell_templates.py`.
    """
    rows = {r["name"]: r for r in live_script_rows}
    for name in ("shell_templates.py", "shell_lint.py"):
        assert rows[name]["indirect_tests"] == "test_validate_shell_templates.py", name


def test_every_inherited_credit_names_a_test_of_a_real_importer(live_script_rows):
    """The `importer` via is exempt from the path-laundering guard, so it needs its own.

    `test_no_script_is_credited_to_another_scripts_own_test` filters `indirect_via == "path"`
    and is therefore silent about this via, which produces exactly the shape it polices:
    `findings_gh.py` credited to `test_findings.py`, and `findings` is a script stem in the
    tree. The exemption is right -- a parent's suite exercises the leaf THROUGH the import,
    where a path mention is only talk -- but that makes the import the thing to check. So
    assert the relationship rather than trusting the via label: the credited
    `test_<X>.py` must name an `X` that really imports this module.
    """
    rows = [r for r in live_script_rows if r["indirect_via"] == "importer"]
    assert rows, "no row uses the importer via -- this guard would pass vacuously"
    imports = sc.importers(g.SCRIPTS)
    unbacked = {
        r["name"]: r["indirect_tests"]
        for r in rows
        if f"{r['indirect_tests'][len('test_') : -len('.py')]}.py"
        not in imports.get(Path(r["name"]).stem, set())
    }
    assert unbacked == {}


def test_inheritance_does_not_require_a_single_importer(tmp_path):
    """The one-importer gate is on the TIE-BREAK, not on the inherited credit.

    Worth pinning because the two rules sit next to each other and read alike. `_name_rank`
    demands exactly one importer, because it REORDERS suites that already cover the module
    and a wrong reorder replaces a right answer. Inheritance runs only after every other
    match has failed, where the alternative is not a different suite but "untested" -- so
    the gate would buy nothing and would cost the understatement the rule exists to fix.
    `infra_map/constants.py` has five importers and is the live case.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "leaf.py", '"""Summary."""\n')
    for caller in ("beta", "alpha"):
        _write(scripts / "pkg" / f"{caller}.py", '"""x"""\nfrom pkg import leaf\n')
        _write(scripts / "pkg" / "tests" / f"test_{caller}.py", "x = 1\n")
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_via"] == "importer"
    assert rows["leaf.py"]["indirect_tests"] == "test_alpha.py"


def test_the_infra_map_facade_members_inherit_the_facades_suite(live_script_rows):
    """Non-vacuity on the real tree, and the other half of the census in the generator's suite.

    `test_a_facade_only_member_gets_no_by_name_import_credit` there asserts only that these
    three do NOT hold an `import` credit, which is what keeps its accept half honest. That
    leaves what they DO hold unpinned, so assert it here: nothing imports them by name, and
    `test_gen_infra_map.py` reaches them through the facade's re-exports.
    """
    rows = {r["name"]: r for r in live_script_rows}
    for stem in ("constants", "inventory", "model"):
        assert rows[f"{stem}.py"]["indirect_via"] == "importer", stem
        assert rows[f"{stem}.py"]["indirect_tests"] == "test_gen_infra_map.py", stem


def test_a_dotted_path_import_credits_the_suite(tmp_path):
    """The stem can sit INSIDE the module path, not only after `from` or `import`.

    `from deploy_tools.land_lib.landing import Landing` binds only capitalised names, so
    neither the top-level branch nor the `from X import <stem>` branch saw it, and
    `land_lib/landing.py` read untested with its suite green (issue #1169).
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "sub" / "leaf.py", '"""Summary."""\n')
    _write(
        scripts / "pkg" / "tests" / "test_leaf_phases.py",
        "from pkg.sub.leaf import Thing\n",
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_tests"] == "test_leaf_phases.py"
    assert rows["leaf.py"]["indirect_via"] == "import"


def test_a_dotted_path_to_a_longer_name_credits_nothing(tmp_path):
    """The RED half: the stem still has to be a whole path segment.

    `from pkg.sub.leaf_helpers import x` names a different module, and crediting it would
    hand a leaf its neighbour's suite -- the miscredit this page has paid for repeatedly.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "pkg" / "sub" / "leaf.py", '"""Summary."""\n')
    _write(scripts / "pkg" / "sub" / "leaf_helpers.py", '"""Summary."""\n')
    _write(
        scripts / "pkg" / "tests" / "test_helpers.py",
        "from pkg.sub.leaf_helpers import Thing\n",
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["leaf.py"]["indirect_tests"] == ""
    assert rows["leaf.py"]["indirect_via"] == ""


def test_the_land_lib_modules_imported_by_dotted_path_are_not_reported_untested(
    live_script_rows,
):
    """Non-vacuity for the dotted-path branch, against the real tree.

    Named members rather than a count, so a failure says which module lost its credit.
    `landing.py` is the dotted-path branch on its own: it held no credit at all before the
    branch existed.

    `ledger.py` pins the tie-break the branch feeds, not the branch itself. It held
    `test_probe_b2_ledger.py`, which imports probe's `b2_ledger` under the alias `ledger`;
    once its own suite matches too, both carry the stem and `deploy_tools` sorting before
    `diagnostics` settles it. So a rename of either suite fails here without the
    dotted-path match having regressed -- read the failure before concluding it has.
    """
    rows = {r["path"]: r for r in live_script_rows}
    expected = {
        "scripts/deploy_tools/land_lib/landing.py": "test_land_landing.py",
        "scripts/deploy_tools/land_lib/ledger.py": "test_land_ledger.py",
    }
    for path, suite in expected.items():
        assert rows[path]["indirect_via"] == "import", path
        assert rows[path]["indirect_tests"] == suite, path
