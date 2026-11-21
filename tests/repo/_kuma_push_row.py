"""The census row holding the Uptime Kuma push out of everything the repo ships or runs."""

import re

from _row_table import Census, Subject, lines_matching, tracked
from dev.affected_lib.table import TABLE_PATH
from lib.repo_paths import REPO

_KUMA_PUSH = re.compile(r"kuma[_-]push|KUMA_PUSH|uptime-kuma-alive")
_TEST_PATH = re.compile(r"(^|/)tests/|(^|/)test_[^/]*\.py$")
_ROOTS = ("hosts/*", "k8s/*", "scripts/*", "inventory/*", ".claude/*", ".github/*")
AFFECTED_TABLE = TABLE_PATH.relative_to(REPO).as_posix()


def _shipped_and_tooling_files() -> list[str]:
    # Tests keep push fixtures, and docs and the archive keep the history. The affected-test
    # table lists test paths, this row's own `_kuma_push_row.py` among them.
    return [
        rel
        for rel in tracked(*_ROOTS, "evals/*", "pyproject.toml", "prek.toml")
        if not _TEST_PATH.search(rel) and rel != AFFECTED_TABLE
    ]


NOTHING_SHIPPED_PUSHES_TO_UPTIME_KUMA = Census(
    name="nothing-shipped-pushes-to-uptime-kuma",
    reason=(
        "Uptime Kuma's tiles, its push library and its push tokens are deleted. A script, "
        "unit or env that pushes to a tile gets a 404 on every run, and one that sources "
        "`kuma-push-lib.sh` fails where it runs. Write the verdict to the check textfile "
        "instead (`write_check_textfile`), or export it from the bridge."
    ),
    files=_shipped_and_tooling_files,
    offence=lines_matching(_KUMA_PUSH),
    red=(
        Subject("a.sh.j2", 'kuma_push "$STATUS" "$MSG" "$PUSH_URL"\n'),
        Subject("b.sh.j2", "source /usr/lib/homelab/kuma-push-lib.sh\n"),
        Subject("c.env.j2", "KUMA_PUSH_URL={{ token }}\n"),
        Subject("d.sh.j2", "ping uptime-kuma-alive\n"),
    ),
    green=(
        Subject("a.sh.j2", "write_check_textfile disk-health 1\n"),
        Subject("b.py", "# Alertmanager pages; the bridge exported to Kuma once.\n"),
    ),
    min_matches=1500,
    must_find=frozenset(
        {
            "hosts/components/pi-heartbeats/component.yml",
            "k8s/roles/monitor-bridge/templates/deployment.yaml.j2",
            "scripts/validate/vale.sh",
        }
    ),
    allow={
        "scripts/secrets_mgmt/git_dates.py": "names the retired push tokens to date them",
        "evals/history.json": "a recorded eval case id",
    },
)
