"""The census row that every tracked caller of `deploy.sh` is in D2's entry-point table.

Target slice D2's PR D2.1b (docs/target-architecture/deploy/d2-first-leaf.md, *Risks*, *An
entry point outside the census*). `deploy.sh` refuses a whole `--tags` list with exit 5 when
one tag is applied by Flux, so each caller has to say what it does with such a tag. The row
sits in its own module because `test_census_rows_text.py`, which imports it into `ROWS`, is
near the cap for a test module.

Baseline item 5 of the plan names `render_targets.py` and `secret_rotation.py:cmd_rotate` as
callers. Neither runs the wrapper: `render_records.py` runs it over `render_targets.py`'s
list, and `rotation_tools.run_deploy` runs `ansible-playbook ansible/deploy.yml` itself, which
D2.1d's `ansible_owned` batch loop governs instead (#4960).
"""

import re

from _row_table import Census, Subject, tracked

# Three spellings of a run, not a mention: an argv list element naming the shim in Python, a
# shell line that starts with it, and a YAML or unit key whose command it is. An alert, a hint
# or a docstring that tells an operator to run `./scripts/deploy.sh` matches none.
_DEPLOY_SH_ARGV = re.compile(r"""["']\./scripts/deploy\.sh["']\s*[,\]]""")
_DEPLOY_SH_SHELL = re.compile(
    r"^[ \t]*(?:exec[ \t]+)?(?:\./|\S*/)?scripts/deploy\.sh(?:\s|$)", re.MULTILINE
)
_DEPLOY_SH_KEY = re.compile(
    r"^[ \t]*-?[ \t]*(?:cmd|command|shell|argv|ExecStart)[ \t]*[:=][^\n]*scripts/deploy\.sh",
    re.MULTILINE,
)
_SHELL_FILES = ("*.sh", "*.sh.j2", "*.bash")
_KEY_FILES = ("*.yml", "*.yaml", "*.service.j2")
_NOT_TESTS = (":(exclude)*/tests/*", ":(exclude)ansible/tests/*")

# Each caller, mapped to its row in the entry-point table of
# docs/target-architecture/deploy/d2-first-leaf.md, *What each entry point does with a tag that
# is not Ansible-owned*. The row says what the caller does with a flux-owned tag, which
# `deploy.sh` refuses with exit 5 for the whole list.
DEPLOY_SH_ENTRY_POINTS = {
    "scripts/deploy_tools/land_lib/tools.py": "land.sh",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py": "deploy_ui",
    "ansible/roles/setup/render_records/files/render_records.py": (
        "render_targets.py CLI, whose list render_records runs"
    ),
}
DEPLOY_SH_EXEMPT = {
    "ansible/bring-up.sh": (
        "runs a full deploy with no --tags, so no tag reaches the refusal; the play's "
        "`ansible_owned` batch loop skips the entry instead"
    ),
}


def _deploy_sh_candidates() -> list[str]:
    return tracked("*.py", *_SHELL_FILES, *_KEY_FILES, *_NOT_TESTS)


def _deploy_sh_runs(subject: Subject) -> int:
    rel = subject.rel
    if rel.endswith(".py"):
        pattern = _DEPLOY_SH_ARGV
    elif rel.endswith(tuple(glob.lstrip("*") for glob in _SHELL_FILES)):
        pattern = _DEPLOY_SH_SHELL
    else:
        pattern = _DEPLOY_SH_KEY
    return len(pattern.findall(subject.text))


def _unlisted_deploy_sh_caller(subject: Subject) -> list[str]:
    if not _deploy_sh_runs(subject) or subject.rel in DEPLOY_SH_ENTRY_POINTS:
        return []
    return ["runs deploy.sh and is in no row of D2's entry-point table"]


DEPLOY_SH_CALLERS = Census(
    name="deploy-sh-callers-are-in-the-entry-point-table",
    reason=(
        "`deploy.sh` refuses a whole --tags list with exit 5 when one tag is applied by "
        "Flux, so a caller nobody listed fails outright the day a tag it passes moves to "
        "Flux, and nobody decided what it should do instead. Add the caller to "
        "the entry-point table in docs/target-architecture/deploy/d2-first-leaf.md, say "
        "there what it does with such a tag, and map it in `DEPLOY_SH_ENTRY_POINTS`; or "
        "exempt it in `DEPLOY_SH_EXEMPT` with the reason no tag reaches it."
    ),
    files=_deploy_sh_candidates,
    offence=_unlisted_deploy_sh_caller,
    count=_deploy_sh_runs,
    red=(
        Subject(
            "scripts/new_caller.py",
            'subprocess.run(["./scripts/deploy.sh", "--tags", tag])\n',
        ),
        Subject("scripts/new_caller.sh", "  ./scripts/deploy.sh --tags x\n"),
        Subject(
            "ansible/roles/setup/x/tasks/main.yml",
            "- ansible.builtin.command:\n    cmd: ./scripts/deploy.sh --tags x\n",
        ),
    ),
    green=(
        Subject(
            "scripts/deploy_tools/land_lib/tools.py",
            'argv = ["./scripts/deploy.sh", "--tags", ",".join(tags)]\n',
        ),
        Subject(
            "scripts/hint.py",
            "print('redeploy with `./scripts/deploy.sh --tags %s`' % tag)\n",
        ),
        Subject("scripts/hint.sh", '  echo "then run ./scripts/deploy.sh"\n'),
    ),
    min_matches=len(DEPLOY_SH_ENTRY_POINTS) + len(DEPLOY_SH_EXEMPT),
    must_find=frozenset(DEPLOY_SH_ENTRY_POINTS) | frozenset(DEPLOY_SH_EXEMPT),
    allow=DEPLOY_SH_EXEMPT,
)
