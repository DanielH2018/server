"""Hold the Alertmanager notification template to the text it must render.

amtool's `check-config` accepts a template that parses, whatever it prints. Each `<case>.json`
under the observability role's `tests/alertmanager/` is one alert group in the shape
Alertmanager passes a template, and each `<case>.<define>.txt` beside it is the text that
define of `files/alertmanager/homelab.tmpl` must render for it. A run fails on any difference.

To change the text on purpose, edit `homelab.tmpl`, run `alert_rules.py --write-renders`, and
review the rewritten `.txt` files in the diff. `RENDER_FLOOR` names the groups a run must
find.

`scripts/validate/alert_rules.py` runs `render_problems` in its tool phase, through
`alert_tools.sh`'s amtool. The pytest shards install no amtool, so its tests pass a runner that
answers with the expected text.
"""

import sys
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import K8S_ROLES

ROLE_DIR = K8S_ROLES / "observability"
TEMPLATE_SOURCE = ROLE_DIR / "files" / "alertmanager" / "homelab.tmpl"
RENDER_CASES = ROLE_DIR / "tests" / "alertmanager"
RENDER_DEFINES = ("homelab.title", "homelab.message", "homelab.email")
# The three shapes the template branches on: a group with firing and resolved alerts, a group
# whose alerts share one description, and a resolved group.
RENDER_FLOOR = frozenset({"mixed", "shared-description", "resolved"})

# A tool run: the argv after `alert_tools.sh`, returning (exit code, combined output).
Runner = Callable[[list[str]], tuple[int, str]]


def render_checks(
    template: Path = TEMPLATE_SOURCE, cases: Path = RENDER_CASES
) -> list[tuple[list[str], Path]]:
    """Each notification render to compare: its `alert_tools.sh` argv and its expected text."""
    checks = []
    for data in sorted(cases.glob("*.json")):
        for define in RENDER_DEFINES:
            argv = [
                "amtool",
                "template",
                "render",
                f"--template.glob={template}",
                f"--template.data={data}",
                f'--template.text={{{{ template "{define}" . }}}}',
            ]
            checks.append((argv, cases / f"{data.stem}.{define}.txt"))
    return checks


def render_problems(
    run: Runner,
    template: Path = TEMPLATE_SOURCE,
    cases: Path = RENDER_CASES,
    write: bool = False,
) -> list[str]:
    """A problem for each notification render that differs from its expected text.

    Args:
        run: Runs one tool invocation.
        template: The Go template file amtool renders.
        cases: The directory of `<case>.json` groups and their expected `.txt` renders.
        write: Rewrite each expected file from amtool's output instead of comparing.
    """
    problems = []
    missing = RENDER_FLOOR - {p.stem for p in cases.glob("*.json")}
    if missing:
        problems.append(f"{cases} lacks the alert groups {sorted(missing)}")
    for argv, expected in render_checks(template, cases):
        code, output = run(argv)
        # Trailing blank lines are compared away: Discord and email drop them, and the repo's
        # end-of-file hook rewrites a .txt file to end in exactly one newline.
        output = output.rstrip("\n") + "\n"
        if code != 0:
            problems.append(f"`{' '.join(argv)}` exited {code}:\n{output.rstrip()}")
        elif write:
            expected.write_text(output)
        elif not expected.exists():
            problems.append(
                f"{expected} is missing. Run alert_rules.py --write-renders"
            )
        elif expected.read_text() != output:
            problems.append(
                f"homelab.tmpl renders {expected.name} differently. If the change is meant, "
                "run alert_rules.py --write-renders and review the diff.\n"
                f"--- expected\n{expected.read_text()}\n--- rendered\n{output}"
            )
    return problems
