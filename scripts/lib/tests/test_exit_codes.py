"""`lib/exit_codes.py`: the frozensets and the individual names must not drift apart.

The failure guarded is the one the module exists to close: a contract decoded in two places,
so a change to one copy that misses the other is exactly the drift this module prevents.

Every rule has a reject half, per CLAUDE.md: a set that quietly stopped containing a member
and a set that quietly gained one are both invisible from a `<=` assertion alone.

Run: uv run pytest scripts/lib/tests/test_exit_codes.py
"""

import ast

import pytest
from _row_table import Census, Subject, check, proof_problems, tracked


from lib import exit_codes as ec
from lib import exit_messages
from lib.repo_paths import REPO


@pytest.mark.parametrize(
    "group",
    [
        ("PUBLISH_PUBLISHED", "PUBLISH_STILL_LOCAL", "PUBLISH_PUSHED_NO_PR"),
        (
            "UNLANDED_NOTHING",
            "UNLANDED_ORIGIN_UNREADABLE",
            "UNLANDED_PR_OPEN",
            "UNLANDED_NO_PR",
            "UNLANDED_PR_RED",
        ),
        ("CI_GREEN", "CI_RED", "CI_DISARMED", "CI_PENDING"),
        ("LAND_SETTLED", "LAND_FAILED", "LAND_BAD_ARGS", "LAND_GAVE_UP"),
    ],
)
def test_no_contract_reuses_a_value_within_itself(group):
    """Two names for one integer inside ONE vocabulary is a bug; across vocabularies it is not."""
    values = [getattr(ec, name) for name in group]
    assert len(set(values)) == len(values), dict(zip(group, values, strict=True))


def test_the_importers_take_their_values_from_here():
    """Non-vacuity: the module is pointless if a consumer still carries its own copy."""
    from deploy_tools.land_lib import outcome

    assert outcome.LAND_GAVE_UP is ec.LAND_GAVE_UP


# -- the sysexits spine --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spine"),
    [
        ("LAND_BAD_ARGS", "USAGE_ERROR"),
        ("LAND_GAVE_UP", "TEMP_FAIL"),
        ("CI_PENDING", "TEMP_FAIL"),
        ("FANOUT_PLACE_BAD_ARGS", "USAGE_ERROR"),
        ("FANOUT_PLACE_UNREAD", "TEMP_FAIL"),
        ("FINDINGS_BAD_ARGS", "USAGE_ERROR"),
        ("SECRET_ROTATION_BAD_ARGS", "USAGE_ERROR"),
        ("B2_DRAIN_BAD_ARGS", "USAGE_ERROR"),
        ("RENDER_BAD_ARGS", "USAGE_ERROR"),
    ],
)
def test_each_family_takes_its_usage_and_temp_fail_from_the_spine(name, spine):
    """One coding across the repo: 64 is always a usage error, 75 always a temporary failure.

    `LAND_BAD_ARGS` is the member this was filed for -- it was 2, argparse's own
    code, while every other entry point used 64 for the same answer.
    """
    assert getattr(ec, name) == getattr(ec, spine)


def test_the_spine_holds_the_sysexits_values():
    """A rename inside the module must not silently renumber every family at once."""
    assert (ec.OK, ec.FAILED, ec.USAGE_ERROR, ec.TEMP_FAIL) == (0, 1, 64, 75)


# -- CONTRACTS, the one renderable copy ----------------------------------------------------


def _declared_values(prefix):
    return {
        value
        for name, value in vars(ec).items()
        if name.startswith(prefix) and isinstance(value, int)
    }


@pytest.mark.parametrize(
    ("entry_point", "prefix"),
    [
        ("scripts/deploy_tools/land.sh", "LAND_"),
        ("scripts/deploy_tools/await_ci.py", "CI_"),
        ("scripts/diagnostics/probe.py", "PROBE_"),
        ("scripts/dev/fanout_lib/place.py", "FANOUT_PLACE_"),
        ("scripts/docs/build_docs.py", "BUILD_DOCS_"),
        ("scripts/dev/findings.py", "FINDINGS_"),
        ("scripts/dev/fact_status.py", "FACT_STATUS_"),
        ("scripts/deploy_tools/runbook_gates.py", "RUNBOOK_GATES_"),
        ("scripts/deploy_tools/local_sweep.py", "SWEEP_"),
        ("scripts/secrets_mgmt/secret_rotation.py", "SECRET_ROTATION_"),
        ("scripts/backup/b2_drain.py", "B2_DRAIN_"),
        ("scripts/infra_map/gen_infra_map.py", "INFRA_MAP_"),
        ("scripts/dev/renovate_rebase.py", "RENOVATE_REBASE_"),
        ("scripts/dev/renovate_branch_sweep.py", "RENOVATE_SWEEP_"),
        ("scripts/renderer/homelab_render.py", "RENDER_"),
        ("scripts/renderer/shadow_diff.py", "SHADOW_"),
    ],
)
def test_every_declared_code_has_a_contract_row(entry_point, prefix):
    """A code added above without a row is a code `describe` cannot explain.

    That is the whole failure mode this table replaces: 77 and 78 each reached production as a
    bare integer while every prose copy of the table still listed seven.
    """
    rows = {code.value for code in ec.contract(entry_point)}
    # Non-vacuity: a family with neither constants nor a row would compare two empty sets.
    assert rows, f"{entry_point} has no contract row"
    assert _declared_values(prefix) == rows


def test_publish_prs_two_vocabularies_share_one_contract_row_set():
    """`publish` and `unlanded` reuse 0-3 deliberately, so their rows are merged, not doubled."""
    rows = {code.value for code in ec.contract("scripts/deploy_tools/publish_pr.py")}
    assert rows == _declared_values("PUBLISH_") | _declared_values("UNLANDED_")


def test_every_contract_row_names_a_constant_that_exists():
    for entry_point, codes in ec.CONTRACTS.items():
        for code in codes:
            assert getattr(ec, code.const) == code.value, f"{entry_point}: {code.const}"


def test_describe_renders_the_constant_and_the_value():
    note = exit_messages.describe("scripts/deploy_tools/land.sh", ec.LAND_GAVE_UP)
    assert note is not None
    assert note.startswith("LAND_GAVE_UP (75): ")


def test_describe_returns_none_for_a_code_outside_the_contract():
    """The reject half: a wrapper must print the bare code rather than a wrong meaning."""
    assert exit_messages.describe("scripts/deploy_tools/land.sh", 200) is None
    assert exit_messages.describe("scripts/nothing.sh", 1) is None


def test_every_contract_key_is_a_tracked_file():
    """A key naming a moved script is a contract nothing renders and `describe` never finds."""
    assert [key for key in ec.CONTRACTS if not (REPO / key).is_file()] == []


def test_the_place_docstring_lists_every_code_its_contract_declares():
    """`fanout.py place --help` prints the docstring, so a code missing from it is unexplained."""
    from fanout_lib import place

    doc = place.__doc__ or ""
    missing = [
        code.value
        for code in ec.contract("scripts/dev/fanout_lib/place.py")
        if f"- {code.value}: " not in doc
    ]
    assert missing == []


# -- die ------------------------------------------------------------------------------------


def test_die_prints_the_detail_and_the_codes_meaning_then_exits_with_it(capsys):
    with pytest.raises(SystemExit) as exc:
        exit_messages.die(
            "scripts/backup/b2_drain.py", ec.B2_DRAIN_BAD_ARGS, "no volumes were named"
        )
    assert exc.value.code == 64
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "b2_drain.py: error: no volumes were named"
    assert err[1].startswith("B2_DRAIN_BAD_ARGS (64): ")


def test_die_prints_no_meaning_for_a_code_outside_the_contract(capsys):
    with pytest.raises(SystemExit) as exc:
        exit_messages.die("scripts/nothing.py", 9, "boom")
    assert exc.value.code == 9
    assert capsys.readouterr().err == "nothing.py: error: boom\n"


# -- the census: argparse's 2 and bare 2s stay out of entry points -------------------------


def _is_two(args) -> bool:
    return bool(args) and isinstance(args[0], ast.Constant) and args[0].value == 2


def _usage_two_offence(subject: Subject) -> list[str]:
    """A bare `argparse.ArgumentParser(...)`, a `return 2`, or a `sys.exit(2)`/`SystemExit(2)`."""
    try:
        tree = ast.parse(subject.text)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and _is_two([node.value]):
            hits.append(f"line {node.lineno}: return 2")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "SystemExit"
            and _is_two(node.args)
        ):
            hits.append(f"line {node.lineno}: SystemExit(2)")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "exit"
            and _is_two(node.args)
        ):
            hits.append(f"line {node.lineno}: exit(2)")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "ArgumentParser"
            and isinstance(func.value, ast.Name)
            and func.value.id == "argparse"
        ):
            hits.append(f"line {node.lineno}: argparse.ArgumentParser(")
    return hits


USAGE_IS_64 = Census(
    name="entry-points-exit-64-on-a-usage-error",
    reason=(
        "argparse exits 2 on a usage error, and entry points here also return 2 as an outcome "
        "(CI_DISARMED, PUBLISH_PUSHED_NO_PR, DEPLOY_TAG_MISS), so a mistyped flag read as a "
        "result (#4443). Build parsers with `lib.cli_help.ArgumentParser`, which exits 64, and "
        "return a named `lib.exit_codes` constant rather than a literal 2."
    ),
    files=lambda: [
        rel
        for rel in tracked("scripts/*.py")
        if "/tests/" not in rel and "/job_files/" not in rel
    ],
    offence=_usage_two_offence,
    red=(
        Subject("a.py", "import argparse\np = argparse.ArgumentParser()\n"),
        Subject("b.py", "def main():\n    return 2\n"),
        Subject("c.py", "import sys\nsys.exit(2)\n"),
        Subject("d.py", "raise SystemExit(2)\n"),
    ),
    green=(
        Subject(
            "a.py", "from lib.cli_help import ArgumentParser\np = ArgumentParser()\n"
        ),
        Subject("b.py", "def main():\n    return USAGE_ERROR\n"),
        Subject(
            "c.py", "import argparse\nclass P(argparse.ArgumentParser):\n    pass\n"
        ),
        Subject("d.py", '"""argparse exits 2: `sys.exit(2)`."""\n'),
    ),
    min_matches=200,
    must_find=frozenset({"scripts/dev/fanout_lib/place.py", "scripts/lib/cli_help.py"}),
)


def test_no_entry_point_exits_2_for_a_usage_error_or_an_unnamed_outcome():
    problems = check(USAGE_IS_64)
    assert not problems, "\n".join(problems)


def test_the_usage_census_flags_its_red_subjects_and_passes_its_green_ones():
    assert proof_problems(USAGE_IS_64) == []


def test_lib_argument_parser_exits_64_on_a_bad_flag_and_in_a_subparser(capsys):
    from lib.cli_help import ArgumentParser

    parser = ArgumentParser(prog="tool")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--n", type=int)
    for argv in (["--bogus"], [], ["run", "--n", "x"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == ec.USAGE_ERROR, argv
    assert "tool: error:" in capsys.readouterr().err
