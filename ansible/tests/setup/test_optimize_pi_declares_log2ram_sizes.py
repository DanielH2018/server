#!/usr/bin/env python3
"""The log-RAM budget's premise is declared, not inherited from whatever log2ram ships.

Section 7 of `optimize_pi`'s tasks sizes four separate caps against one number -- the log2ram
tmpfs, 128 MB: journald `SystemMaxUse=32M`, auditd's 12 MB ceiling, `ACCT_LOGGING="3"` and
sysstat `HISTORY=2`. The number itself comes from the package unless the role declares it.
log2ram shipped `SIZE=40M` before it shipped `SIZE=128M`, so a reinstall or a fresh
provision under a moved default runs those caps into a tmpfs a third of the size they assume,
with nothing on the run that says so.

Two things are guarded here, and the second is the one a reader of the task cannot check:

1. The role declares `SIZE` and `LOG_DISK_SIZE` for /etc/log2ram.conf at all.
2. The declared `SIZE` is still larger than the journald cap set beside it. That is the
   relationship the section exists to maintain; either value can be edited alone by someone who
   cannot see the other.

Run: uv run pytest ansible/tests/setup/test_optimize_pi_declares_log2ram_sizes.py
"""

import re
import shutil

import pytest
from _helpers import SETUP_ROLES, load_tasks, load_yaml, walk_tasks
from lib.proc_testing import run

ROLE = SETUP_ROLES / "optimize_pi"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = load_yaml(ROLE / "defaults" / "main.yml")
CONF = "/etc/log2ram.conf"

# The keys the rest of section 7 depends on. COMP_ALG and ZL2R are declared too, but only these
# two are load-bearing: one sizes the tmpfs, the other the zram device that would replace it.
REQUIRED_KEYS = ("SIZE", "LOG_DISK_SIZE")

_MB = {"K": 1 / 1024, "M": 1, "G": 1024}


def _mb(value: str) -> float:
    """`128M` -> 128.0. Raises on a unit this role has never used, rather than guessing."""
    match = re.fullmatch(r"(\d+)([KMG])", value.strip())
    assert match, f"cannot read {value!r} as a size"
    return int(match.group(1)) * _MB[match.group(2)]


def _declared_lines() -> dict[str, str]:
    """Every `<KEY>=<value>` line the role writes into /etc/log2ram.conf, keyed by KEY."""
    declared = {}
    for task in walk_tasks(load_tasks(TASKS)):
        spec = task.get("ansible.builtin.lineinfile")
        if not isinstance(spec, dict) or spec.get("path") != CONF:
            continue
        for item in task.get("loop") or [spec]:
            line = str(item.get("line", spec.get("line", "")))
            if "=" in line:
                key, _, value = line.partition("=")
                declared[key.strip()] = value.strip()
    return declared


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_role_declares_the_log2ram_size(key):
    declared = _declared_lines()
    assert key in declared, (
        f"optimize_pi does not declare {key} in {CONF}. The caps in section 7 are computed from "
        f"it, and the package default has moved before (40M -> 128M). Declared: {sorted(declared)}"
    )
    assert declared[key].startswith("{{"), (
        f"{key} is hard-coded as {declared[key]!r}; it should render the role default so the "
        "number has one home"
    )


def test_the_size_regexps_are_anchored():
    """An unanchored `SIZE=` also matches `LOG_DISK_SIZE=256M` and rewrites the wrong line."""
    regexps = [
        str(item.get("regexp", ""))
        for task in walk_tasks(load_tasks(TASKS))
        if isinstance(spec := task.get("ansible.builtin.lineinfile"), dict)
        and spec.get("path") == CONF
        for item in task.get("loop") or [spec]
    ]
    assert regexps, f"no lineinfile loop writes {CONF}"
    for regexp in regexps:
        assert regexp.startswith("^"), (
            f"{regexp!r} is unanchored: `SIZE=` matches LOG_DISK_SIZE's line too, and lineinfile "
            "rewrites the LAST match"
        )


def test_the_declared_tmpfs_is_larger_than_the_journald_cap_beside_it():
    """The relationship section 7 exists to hold: the caps have to fit in the tmpfs."""
    size_mb = _mb(DEFAULTS["optimize_pi_log2ram_size"])
    # The cap is a line of the drop-in a `copy` task writes, so it is read out of that task's
    # parsed `content`, where a comment in the task file cannot supply it.
    caps = [
        cap
        for task in walk_tasks(load_tasks(TASKS))
        if isinstance(spec := task.get("ansible.builtin.copy"), dict)
        and str(spec.get("dest", "")).startswith("/etc/systemd/journald.conf.d/")
        for cap in re.findall(
            r"^SystemMaxUse=(\d+[KMG])$", str(spec.get("content", "")), re.M
        )
    ]
    assert caps, "no journald SystemMaxUse found in the role -- section 7 moved"
    for cap in caps:
        assert _mb(cap) < size_mb, (
            f"journald is capped at {cap} inside a {DEFAULTS['optimize_pi_log2ram_size']} tmpfs; "
            "one of the two moved without the other"
        )


def test_the_size_reader_rejects_a_unit_it_does_not_know():
    """The RED half: without this, a typo'd `128MB` would read as some number and compare true."""
    with pytest.raises(AssertionError):
        _mb("128MB")


# ── The duplicate JOURNALD_AWARE line ─────────────────────────────────────────


def _journald_guard() -> dict:
    """The task that reads JOURNALD_AWARE out of /etc/log2ram.conf, or {} if it is gone."""
    for task in walk_tasks(load_tasks(TASKS)):
        spec = task.get("ansible.builtin.command")
        argv = spec.get("argv") if isinstance(spec, dict) else None
        if argv and any("JOURNALD_AWARE" in str(arg) for arg in argv):
            return task
    return {}


def _awk_program(task: dict) -> str:
    """The awk program the role actually ships, so the RED half below cannot drift from it."""
    argv = [str(arg) for arg in task["ansible.builtin.command"]["argv"]]
    return next(arg for arg in argv if "JOURNALD_AWARE" in arg)


def test_the_role_checks_the_duplicate_journald_aware_lines_agree():
    """log2ram 1.7.2 ships JOURNALD_AWARE twice. Both copies read `true`, log2ram sources the
    file, so the duplicate is inert and the role leaves the conffile dpkg-pristine rather than
    deleting a shipped line. Two copies that DISAGREE are a real signal — the last one silently
    wins — and that is what this task fails on.
    """
    # fact: ansible/roles/setup/optimize_pi/CLAUDE.md#What it does (`tasks/main.yml`)
    task = _journald_guard()
    assert task, (
        f"no task reads JOURNALD_AWARE from {CONF}. The role leaves upstream's duplicate line "
        "in place on purpose (#2726); the check that the copies agree is what makes that safe"
    )
    argv = [str(arg) for arg in task["ansible.builtin.command"]["argv"]]
    assert CONF in argv, f"the guard does not read {CONF}: {argv}"
    assert task.get("changed_when") is False, (
        "a read-only check must not report changed"
    )
    assert "!= 1" in str(task.get("failed_when", "")), (
        "the guard must fail on anything but ONE distinct value: 2+ means the copies disagree, "
        f"0 means the anchor stopped matching. failed_when: {task.get('failed_when')!r}"
    )


@pytest.mark.skipif(shutil.which("awk") is None, reason="the guard's program is awk")
@pytest.mark.parametrize(
    ("conf", "distinct"),
    [
        (
            "JOURNALD_AWARE=true\n#x\nJOURNALD_AWARE=true\n",
            1,
        ),  # upstream 1.7.2, as shipped
        ("JOURNALD_AWARE=true\n", 1),  # upstream master, and any later fixed release
        (
            "JOURNALD_AWARE=true\nJOURNALD_AWARE=false\n",
            2,
        ),  # RED: the last one silently wins
        ("SIZE=128M\n", 0),  # RED: the anchor matches nothing and would check nothing
    ],
)
def test_the_guards_awk_program_counts_distinct_values(tmp_path, conf, distinct):
    """The verdict half, run through the program the task ships rather than a retyped copy."""
    conf_file = tmp_path / "log2ram.conf"
    conf_file.write_text(conf)
    out = run(
        ["awk", "-F=", _awk_program(_journald_guard()), str(conf_file)], check=True
    )
    assert int(out.stdout.strip()) == distinct
