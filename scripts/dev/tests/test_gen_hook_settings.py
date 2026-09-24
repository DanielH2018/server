"""Tests for gen_hook_settings, the `hooks`-key generator for `.claude/settings.json`.

The generator exists to make one state unlandable: a hook file under `.claude/hooks/` that
no event registers. So the tests here are the red proofs for that verdict -- an undeclared
file fails the census, a malformed block fails the parse, a duplicate order fails the render
-- each paired with the accepting input, plus the identity proof that the committed
`settings.json` is what the generator renders from the committed hook files. The live-tree
tests assert named members so a census that quietly emptied fails by name.

Run: uv run pytest scripts/dev/tests/test_gen_hook_settings.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import gen_hook_settings as g

# Named members of the live census. A `.sh` here is registered; a `.py` is a library (run
# by its `.sh` shim through `uv run python`, or imported by a sibling, never registered).
KNOWN_REGISTERED = frozenset(
    {
        "bash-pretool.sh",
        "uv-python.sh",
        "block-protected-edits.sh",
        "ansible-lint.sh",
        "validate-compose.sh",
        "auto-mode-bridge.sh",
        "log-instructions.sh",
        "session-health.sh",
    }
)
KNOWN_LIBRARIES = frozenset(
    {
        "_claude_guard.py",
        "_hook_common.py",
        "bash-pretool.py",
        "session-health.py",
    }
)


def register(body: str) -> str:
    return f"#!/bin/bash\n# gen-hooks: register\n{body}\necho hi\n"


DECLARED = register("#   event: Stop\n#   timeout: 5\n#   order: 10")
LIBRARY = "#!/usr/bin/env python3\n# gen-hooks: library\n#   reason: run by x.sh\n"


# --- parse_hook_file ---------------------------------------------------------------------


def test_register_block_derives_the_command_and_stops_at_the_first_prose_line():
    text = register(
        "#   event: SessionStart\n#   matcher: startup|resume\n#   timeout: 5\n"
        "#   order: 20\n#   args: --banner\n#   async: true\n"
        "#   statusMessage: Checking...\n# prose after the block is not a field"
    )
    parsed = g.parse_hook_file("warp.sh", text)
    assert parsed.library_reason is None
    [r] = parsed.registrations
    assert (r.event, r.matcher, r.timeout, r.order) == (
        "SessionStart",
        "startup|resume",
        5,
        20,
    )
    assert r.command == "~/server/.claude/hooks/warp.sh --banner"
    assert r.entry() == {
        "type": "command",
        "command": "~/server/.claude/hooks/warp.sh --banner",
        "timeout": 5,
        "statusMessage": "Checking...",
        "async": True,
    }


def test_a_file_may_carry_several_register_blocks():
    text = register("#   event: Stop\n#   timeout: 5\n#   order: 10") + register(
        "#   event: SessionStart\n#   timeout: 5\n#   order: 10"
    )
    events = [r.event for r in g.parse_hook_file("two.sh", text).registrations]
    assert events == ["Stop", "SessionStart"]


def test_library_block_is_accepted_with_a_reason():
    parsed = g.parse_hook_file("lib.py", LIBRARY)
    assert parsed.registrations == []
    assert parsed.library_reason == "run by x.sh"


@pytest.mark.parametrize(
    ("label", "text", "message"),
    [
        (
            "unknown opener",
            "#!/bin/bash\n# gen-hooks: registr\n#   event: Stop\n",
            "unknown block",
        ),
        (
            "unknown key",
            register("#   event: Stop\n#   timeout: 5\n#   order: 10\n#   timeot: 5"),
            "unknown key 'timeot'",
        ),
        (
            "missing timeout",
            register("#   event: Stop\n#   order: 10"),
            "missing timeout:",
        ),
        (
            "unknown event",
            register("#   event: OnStop\n#   timeout: 5\n#   order: 10"),
            "unknown event 'OnStop'",
        ),
        (
            "non-integer order",
            register("#   event: Stop\n#   timeout: 5\n#   order: first"),
            "order must be a non-negative integer",
        ),
        (
            "duplicate key",
            register("#   event: Stop\n#   event: Stop\n#   timeout: 5\n#   order: 1"),
            "duplicate key 'event'",
        ),
        (
            "async not true",
            register("#   event: Stop\n#   timeout: 5\n#   order: 1\n#   async: yes"),
            "async: takes only 'true'",
        ),
        (
            "library without reason",
            "#!/bin/bash\n# gen-hooks: library\n",
            "needs a reason:",
        ),
        (
            "library and register in one file",
            LIBRARY + register("#   event: Stop\n#   timeout: 5\n#   order: 10"),
            "marked library but also",
        ),
    ],
)
def test_a_malformed_block_is_flagged(label, text, message):
    with pytest.raises(g.HookDeclarationError, match=message):
        g.parse_hook_file("bad.sh", text)


def test_a_file_with_no_block_parses_to_nothing():
    """The parser is not the gate; census() is. A silent file parses clean."""
    parsed = g.parse_hook_file("silent.sh", "#!/bin/bash\necho nothing\n")
    assert parsed.registrations == [] and parsed.library_reason is None


# --- census: the verdict the generator exists for ----------------------------------------


def test_census_flags_a_file_that_declares_nothing_by_name():
    with pytest.raises(g.HookDeclarationError, match=r"1 hook file\(s\).*silent\.sh"):
        g.census({"declared.sh": DECLARED, "silent.sh": "#!/bin/bash\necho\n"})


def test_census_is_clean_when_every_file_declares_something():
    registrations, libraries = g.census({"declared.sh": DECLARED, "lib.py": LIBRARY})
    assert [r.file for r in registrations] == ["declared.sh"]
    assert libraries == {"lib.py": "run by x.sh"}


def test_hook_files_reads_sh_and_py_one_level_only_regardless_of_the_exec_bit(
    tmp_path: Path,
):
    """A hook committed without its exec bit is still a hook file (#361), so the bit does not
    decide; `tests/` and `hooklib/` sit one level down and are reached by import, never by
    a registration; a README beside the hooks is not a hook either."""
    (tmp_path / "a.sh").write_text(DECLARED)
    (tmp_path / "a.sh").chmod(0o755)
    (tmp_path / "forgot-chmod.sh").write_text(DECLARED)
    (tmp_path / "_helper.py").write_text(LIBRARY)
    (tmp_path / "README.md").write_text("not a hook\n")
    (tmp_path / "hooklib").mkdir()
    (tmp_path / "hooklib" / "d.py").write_text("x = 1\n")
    assert set(g.hook_files(tmp_path)) == {"a.sh", "forgot-chmod.sh", "_helper.py"}


# --- render ------------------------------------------------------------------------------


def _reg(file: str, event: str, order: int, matcher: str | None = "Bash"):
    return g.Registration(
        file=file, event=event, timeout=10, order=order, matcher=matcher
    )


def test_render_sorts_by_order_and_folds_consecutive_entries_sharing_a_matcher():
    hooks = g.render_hooks(
        [
            _reg("second.sh", "PreToolUse", 20),
            _reg("edits.sh", "PreToolUse", 30, matcher="Edit|Write"),
            _reg("first.sh", "PreToolUse", 10),
            _reg("start.sh", "SessionStart", 10, matcher=None),
        ]
    )
    assert list(hooks) == ["PreToolUse", "SessionStart"]
    groups = hooks["PreToolUse"]
    assert [grp.get("matcher") for grp in groups] == ["Bash", "Edit|Write"]
    assert [h["command"].rsplit("/", 1)[1] for h in groups[0]["hooks"]] == [
        "first.sh",
        "second.sh",
    ]
    assert "matcher" not in hooks["SessionStart"][0]


def test_render_flags_a_duplicate_order_within_an_event():
    with pytest.raises(g.HookDeclarationError, match="both declare order: 10"):
        g.render_hooks([_reg("a.sh", "PreToolUse", 10), _reg("b.sh", "PreToolUse", 10)])


def test_render_settings_replaces_only_the_hooks_key():
    before = json.dumps(
        {"permissions": {"allow": ["Bash(ls *)"]}, "hooks": {}, "z": 1}, indent=2
    )
    after = g.render_settings(before + "\n", [_reg("a.sh", "PreToolUse", 10)])
    data = json.loads(after)
    assert list(data) == ["permissions", "hooks", "z"]
    assert data["permissions"] == {"allow": ["Bash(ls *)"]}
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("/a.sh")
    assert after.endswith("}\n")


# --- the live tree -----------------------------------------------------------------------


def test_live_census_names_the_known_members():
    registrations, libraries = g.census(g.hook_files())
    registered = {r.file for r in registrations}
    assert KNOWN_REGISTERED <= registered, sorted(KNOWN_REGISTERED - registered)
    assert KNOWN_LIBRARIES <= set(libraries), sorted(KNOWN_LIBRARIES - set(libraries))
    assert registered.isdisjoint(libraries)


def test_committed_settings_json_is_what_the_generator_renders_now():
    """The identity proof: `--check` is green on the committed tree, so a hand edit to
    either side fails it."""
    assert g.main(["--check"]) == 0


def test_check_goes_red_on_an_undeclared_hook_file(tmp_path: Path, capsys):
    """The red proof the brief asks for: one silent file beside the real ones."""
    hooks = tmp_path / "hooks"
    shutil.copytree(g.HOOKS_DIR, hooks)
    (hooks / "orphan.sh").write_text("#!/bin/bash\necho nothing declared\n")
    settings = tmp_path / "settings.json"
    settings.write_text(g.SETTINGS.read_text())
    assert g.main(["--check"], hooks_dir=hooks, settings=settings) == 1
    assert "orphan.sh" in capsys.readouterr().err


def test_check_goes_red_when_settings_json_drifts_from_the_declarations(tmp_path: Path):
    settings = tmp_path / "settings.json"
    data = json.loads(g.SETTINGS.read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] += 1
    settings.write_text(json.dumps(data, indent=2) + "\n")
    assert g.main(["--check"], settings=settings) == 1
    # The writer repairs it, and the repaired file passes.
    assert g.main([], settings=settings) == 0
    assert g.main(["--check"], settings=settings) == 0


def test_the_entry_point_runs_from_its_own_directory():
    """The `sys.path` bootstrap, exercised the way prek runs it rather than through pytest's
    `pythonpath`, which is exactly what cannot see a missing bootstrap."""
    proc = subprocess.run(
        [sys.executable, str(g.REPO / g.SELF), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up to date" in proc.stdout
