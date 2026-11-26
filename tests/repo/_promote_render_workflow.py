"""What the promote workflow's bundle, schedule and uv rows read: constants, judges and mutations.

`_promote_render_workflow_rows.py` holds the rows. `_promote_workflow.py` holds the jobs,
guards and helpers every promote row module shares.
"""

import re

from _promote_workflow import job
from _workflow_rows import (
    Workflow,
    condition,
    index_of,
    jobs,
    step_index,
    steps,
    uses,
)

CENSUS = "promote.py check-bundle out --against-prod"
# `images` refuses a render without tags.json and `publish` needs `images`, so the no-tag branch
# passes --all-images too.
SHARED_ONLY = "homelab_render.py cluster --out out --shared-only --all-images"
# A `--role` render writes only the named tags' image contexts, and `images` builds every one.
ALL_IMAGES = 'homelab_render.py cluster --out out --all-images "${roles[@]}"'
NO_TAG = 'if [ -z "$listed" ]; then'
CIPHERTEXT = "cluster_vars.py check --ciphertext-only"

SCHEDULED = "github.event_name == 'schedule'"
SCHEDULED_PROMOTE = f"({SCHEDULED} && needs.publish.result == 'skipped')"
# A status-check function in an `if` replaces the implicit success(); only !cancelled() is accepted,
# as always() would also move `prod` on a cancelled run.
STATUS_CHECK = "!cancelled()"
# `tag` reads `promote`'s result itself, because the skipped publish is its ancestor too.
TAG_RESULT = (STATUS_CHECK, "needs.promote.result == 'success'")
AFTER_PROMOTE = ("tag", "verify", "settle", "post")
# `decide` reads the window in `promote`, and the jobs after it act on its verdict.
WINDOW_JOBS = frozenset({"render", "images", "publish", "promote", *AFTER_PROMOTE})
RESET_JOBS = ("reset-check", "reset-move")

DEV_GROUP_FLAG = re.compile(
    r"\buv (?:run|sync)\b[^\n]*--(?:dev|only-dev|group|all-groups)\b"
)


def _render(wf: Workflow) -> dict:
    return job(wf, "render")


def _run(wf: Workflow, step_id: str) -> str:
    render = _render(wf)
    index = step_index(render, step_id)
    return "" if index is None else str(steps(render)[index].get("run", ""))


def build_script(wf: Workflow) -> str:
    return _run(wf, "build")


def no_tag_branch(script: str) -> list[str]:
    """The stripped lines of the build script's no-tag branch, up to its `else`."""
    _, found, rest = script.partition(NO_TAG)
    lines = []
    for line in rest.splitlines() if found else []:
        if line.strip() in ("else", "fi"):
            break
        lines.append(line.strip())
    return lines


def bundle_step_problems(step_id: str, command: str):
    """A judge: render runs `command` in the step `step_id`, between the build and gitleaks."""

    def rule(wf: Workflow) -> list[str]:
        render = _render(wf)
        position, build, scan = (
            step_index(render, name) for name in (step_id, "build", "gitleaks")
        )
        if command not in _run(wf, step_id):
            return [f"render: runs no `{command}` step with id `{step_id}`"]
        if position is None or build is None or scan is None:
            return [f"render: has no step with id `{step_id}`, `build` or `gitleaks`"]
        if not build < position < scan:
            return [
                f"render: does not run {step_id} after the build and before gitleaks"
            ]
        return []

    return rule


def upload_problems(wf: Workflow) -> list[str]:
    render = _render(wf)
    upload = next(
        (i for i, s in enumerate(steps(render)) if uses(s, "actions/upload-artifact")),
        None,
    )
    needs = jobs(wf).get("publish", {}).get("needs")
    if upload is None or "render" not in (
        [needs] if isinstance(needs, str) else needs or []
    ):
        return ["render: hands out/ to no publish job that needs it"]
    checks = (
        step_index(render, name) for name in ("check-bundle", "ciphertext", "gitleaks")
    )
    if any(i is None or i > upload for i in checks):
        return [
            "render: hands out/ on before check-bundle, the ciphertext check or gitleaks runs"
        ]
    return []


def stub_mode_problems(wf: Workflow) -> list[str]:
    if "--secret-stubs" in build_script(wf):
        return ["render: the build step passes --secret-stubs, a stub-mode render"]
    return []


def history_problems(wf: Workflow) -> list[str]:
    depths = [
        (s.get("with") or {}).get("fetch-depth")
        for s in steps(job(wf, "promote"))
        if uses(s, "actions/checkout")
    ]
    if depths != [0]:
        return [
            "promote: does not fetch the history decide reads trailers from "
            f"(fetch-depth {depths})"
        ]
    return []


def no_tag_branch_problems(wf: Workflow) -> list[str]:
    if f"uv run python scripts/renderer/{SHARED_ONLY}" not in no_tag_branch(
        build_script(wf)
    ):
        return [f"render: the build step's no-tag branch does not run `{SHARED_ONLY}`"]
    return []


def tag_branch_problems(wf: Workflow) -> list[str]:
    if ALL_IMAGES not in build_script(wf):
        return [f"render: the build step's tag branch does not run `{ALL_IMAGES}`"]
    return []


def window_end_promote_problems(wf: Workflow) -> list[str]:
    cond = condition(job(wf, "promote"))
    found = []
    if SCHEDULED_PROMOTE not in cond:
        found.append(f"promote: its `if` lacks {SCHEDULED_PROMOTE}")
    if STATUS_CHECK not in cond:
        found.append(
            f"promote: its `if` lacks {STATUS_CHECK}, so the scheduled run skips it"
        )
    return found


def window_end_tag_problems(wf: Workflow) -> list[str]:
    return [
        f"tag: its `if` lacks {guard}, so the scheduled run skips it"
        for guard in TAG_RESULT
        if guard not in condition(job(wf, "tag"))
    ]


def after_promote_problems(wf: Workflow) -> list[str]:
    return [
        f"{name}: its `if` lacks {SCHEDULED}"
        for name in AFTER_PROMOTE
        if SCHEDULED not in condition(job(wf, name))
    ]


def render_schedule_problems(wf: Workflow) -> list[str]:
    if SCHEDULED in condition(_render(wf)):
        return ["render: runs on the schedule, so it publishes an unchecked tip"]
    return []


def _ancestors(wf: Workflow, name: str) -> set[str]:
    needs = job(wf, name).get("needs") or []
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_ancestors(wf, n) for n in direct))


def reset_window_problems(wf: Workflow) -> list[str]:
    found = []
    for name in RESET_JOBS:
        if name not in jobs(wf):
            found.append(f"{name}: the job is missing")
        elif crossed := sorted(_ancestors(wf, name) & WINDOW_JOBS):
            found.append(f"{name}: needs {crossed}, which read the reboot window")
    return found


def uv_env_problems(wf: Workflow) -> list[str]:
    env = wf.get("env") or {}
    found = []
    if env.get("UV_NO_DEV") != "1":
        found.append(
            "the workflow env does not set UV_NO_DEV, so `uv run` installs the dev group"
        )
    if env.get("UV_NO_GROUP") != "lint":
        found.append(
            "the workflow env does not set UV_NO_GROUP=lint, so `uv run` installs the lint "
            "group"
        )
    return found


def uv_override_problems(wf: Workflow) -> list[str]:
    return [
        f"{name}: overrides {var}"
        for name, node in jobs(wf).items()
        for scope in [node, *steps(node)]
        for var in ("UV_NO_DEV", "UV_NO_GROUP")
        if var in (scope.get("env") or {})
    ]


def uv_group_flag_problems(wf: Workflow) -> list[str]:
    return [
        f"{name}: a `uv` call asks for a dependency group"
        for name, node in jobs(wf).items()
        for scope in [node, *steps(node)]
        if DEV_GROUP_FLAG.search(str(scope.get("run", "")))
    ]


def replace_in_build(old: str, new: str):
    """A mutation that rewrites the build script, and raises when `old` is not in it."""

    def mutate(wf: Workflow) -> None:
        step = steps(_render(wf))[index_of(_render(wf), "build")]
        if old not in step["run"]:
            raise AssertionError(f"the build script holds no {old!r}")
        step["run"] = step["run"].replace(old, new)

    return mutate


def replace_in_condition(name: str, old: str, new: str):
    def mutate(wf: Workflow) -> None:
        wf["jobs"][name]["if"] = wf["jobs"][name]["if"].replace(old, new)

    return mutate


def move_step_after(step_id: str, anchor_id: str | None):
    """A mutation that moves render's `step_id` just after `anchor_id`, or to the end."""

    def mutate(wf: Workflow) -> None:
        render = steps(_render(wf))
        moved = render.pop(index_of(_render(wf), step_id))
        at = len(render) if anchor_id is None else index_of(_render(wf), anchor_id) + 1
        render.insert(at, moved)

    return mutate


def shared_only_in_the_tag_branch(wf: Workflow) -> None:
    """`--shared-only` stays in the script, on the line that publishes tags."""
    replace_in_build(
        'out --all-images "${roles[@]}"', 'out --shared-only "${roles[@]}"'
    )(wf)
    replace_in_build(
        "cluster --out out --shared-only --all-images\n", "cluster --out out --role x\n"
    )(wf)


def shared_only_line_outside_the_no_tag_branch(where: str):
    """The exact no-tag command line stays in the script, but `where` holds it."""
    literal = f"uv run python scripts/renderer/{SHARED_ONLY}"

    def mutate(wf: Workflow) -> None:
        replace_in_build(
            "cluster --out out --shared-only --all-images\n",
            "cluster --out out --role x\n",
        )(wf)
        if where == "else":
            replace_in_build(
                'out --all-images "${roles[@]}"\n',
                f'out --all-images "${{roles[@]}}"\n{literal}\n',
            )(wf)
        else:
            replace_in_build("\nfi", f"\nfi\n{literal}")(wf)

    return mutate


def shallow_promote(wf: Workflow) -> None:
    for step in wf["jobs"]["promote"]["steps"]:
        if uses(step, "actions/checkout"):
            step["with"]["fetch-depth"] = 1
