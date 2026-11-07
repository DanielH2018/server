"""The census row that every tracked caller of `deploy.sh` says why no Flux tag reaches it.

Target slice D2's PR D2.1b (docs/target-architecture/deploy/d2-first-leaf.md, *Risks*, *An
entry point outside the census*). `deploy.sh` refuses a whole `--tags` list with exit 5 when
one tag is applied by Flux, so each caller has to say what it does with such a tag. The row
sits in its own module because `test_census_rows_text.py`, which imports it into `ROWS`, is
near the cap for a test module. `table_rows` reads D2's entry-point table for the owner census.
"""

import re

from _row_table import Census, Subject, tracked

# Three spellings of a run, not a mention: an argv list element naming the shim in Python, a
# shell line that starts with it, and a YAML or unit key whose command it is. YAML takes the
# shell spelling too, for a command in a block scalar under `shell: |`. An alert, a hint or a
# docstring that names `./scripts/deploy.sh` mid-sentence matches none.
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

PLAN = "docs/target-architecture/deploy/d2-first-leaf.md"
TABLE_HEADING = "### What each entry point does with a tag that is not Ansible-owned"

DEPLOY_SH_EXEMPT = {
    "ansible/bring-up.sh": (
        "runs a full deploy with no --tags, so no tag reaches the refusal; the play's "
        "`ansible_owned` batch loop skips the entry instead"
    ),
}


def table_rows(text: str) -> set[str]:
    """The first cells of the table under `TABLE_HEADING` in `text`, or none without one."""
    _, found, rest = text.partition(TABLE_HEADING)
    section = rest.split("\n#", 1)[0] if found else ""
    return {
        line.split("|")[1].strip()
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith("|-")
    }


def _deploy_sh_candidates() -> list[str]:
    return tracked("*.py", *_SHELL_FILES, *_KEY_FILES, *_NOT_TESTS)


def _deploy_sh_runs(subject: Subject) -> int:
    rel = subject.rel
    if rel.endswith(".py"):
        patterns = (_DEPLOY_SH_ARGV,)
    elif rel.endswith(tuple(glob.lstrip("*") for glob in _SHELL_FILES)):
        patterns = (_DEPLOY_SH_SHELL,)
    else:
        patterns = (_DEPLOY_SH_KEY, _DEPLOY_SH_SHELL)
    return sum(len(pattern.findall(subject.text)) for pattern in patterns)


def _unlisted_deploy_sh_caller(subject: Subject) -> list[str]:
    if not _deploy_sh_runs(subject):
        return []
    return ["runs deploy.sh and is not exempted with a reason no tag reaches it"]


DEPLOY_SH_CALLERS = Census(
    name="deploy-sh-callers-are-in-the-entry-point-table",
    reason=(
        "`deploy.sh` refuses a whole --tags list with exit 5 when one tag is applied by "
        "Flux, so a caller nobody listed fails outright on any tag Flux applies. Exempt "
        "it in `DEPLOY_SH_EXEMPT` with the reason no tag reaches it."
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
        Subject(
            "ansible/roles/setup/y/tasks/main.yml",
            "- ansible.builtin.shell: |\n    ./scripts/deploy.sh --tags x\n",
        ),
    ),
    green=(
        Subject(
            "scripts/hint.py",
            "print('redeploy with `./scripts/deploy.sh --tags %s`' % tag)\n",
        ),
        Subject("scripts/hint.sh", '  echo "then run ./scripts/deploy.sh"\n'),
    ),
    min_matches=len(DEPLOY_SH_EXEMPT),
    must_find=frozenset(DEPLOY_SH_EXEMPT),
    allow=DEPLOY_SH_EXEMPT,
)
