"""The brief a headless fan-out agent reads on stdin — spec 2026-09-06 §3.

A headless agent reads no SessionStart banner, so the brief carries what the banner carries.
The issue-fanout skill's required-contents list (its step 3) is the source; keep the two in step.
"""

from collections.abc import Sequence
from dataclasses import dataclass

LANDS = "daniel-box"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str


def _landing(host: str, batch: str) -> str:
    if host == LANDS:
        return """## Landing
Land with `land.sh` (the `land-after-merge` skill); a hook denies hand-polling CI:

```bash
git rev-parse origin/master
./scripts/deploy_tools/land.sh --pr <n> --since <pre-merge-sha> --arm-merge --await-merge \\
  > "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1
```
Run that backgrounded, then wait in the foreground, once, instead of ending your turn:
```bash
timeout 1200 tail -f -n +1 "$CLAUDE_JOB_DIR/tmp/land<n>.log" | grep -m1 '^VERDICT:'
```
It prints the verdict at once and returns at the timeout; that is not a failure.
`deploy.sh` exit 75 is a resume point to retry, not a failure to report.
Close a fixed issue with exactly `findings.py close <n> --fixed --pr <n>`; `--refuted` and
`--accepted` are operator-only.
"""
    return f"""## Landing
This host is {host}, not the deploy host. Open the PR with `gh pr create` and STOP there:
**do not merge**, do not deploy, do not run the land script. Print the PR URL as the last line
of your final message. A daniel-box session lands it and closes the issue.
"""


def render_brief(
    issues: Sequence[Issue],
    host: str,
    batch: str,
    orchestrator_branch: str,
    health: Sequence[str],
) -> str:
    """Render the stdin brief a headless fan-out agent reads on launch.

    Args:
        issues: the batch's issues, in claim order.
        host: the host the agent runs on — governs the landing instructions.
        batch: the batch id (issue numbers joined by `-`), used to derive the worktree branch.
        orchestrator_branch: the branch the issues are already claimed under.
        health: SessionStart-banner-style health lines to carry through, or empty.

    Returns:
        The full brief text.
    """
    branch = f"worktree-fanout-{batch}"
    numbers = " ".join(str(i.number) for i in issues)
    bodies = "\n\n".join(f"### #{i.number}: {i.title}\n\n{i.body}" for i in issues)
    health_block = "\n".join(health) if health else "(both hosts reported clean)"
    first_act = "\n".join(
        f'gh issue comment {i.number} --body "Worked by `{branch}`"' for i in issues
    )
    return f"""# Fan-out batch {batch} on {host}

You are a headless Opus agent in the worktree `{branch}` of /home/ubuntu/server, checked out
fresh from origin/master. Read CLAUDE.md first. Work the issues below to a PR.

## Claim
Issues {numbers} are already claimed under `{orchestrator_branch}`. Do not claim them again.
Your first act is to record which agent took the work:
```bash
{first_act}
```

## Host state at launch (what the SessionStart banner would have shown)
{health_block}

{_landing(host, batch)}
## Anything you do not fix
File it with `findings.py open` (flags: docs/reference/scripts.md). Never leave it unmentioned.

## Issues
{bodies}
"""
