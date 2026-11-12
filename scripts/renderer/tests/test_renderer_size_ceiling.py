"""The renderer's non-test line count stays at or under the ceiling the operator accepted.

R4 exit 6 bounded the renderer at 2,600 lines and it read 13,549. The operator accepted the
overrun on 2026-11-07 ("accept the cap overrun"). The ceiling is the size measured when R4.9
merged, 13,546 lines, so the renderer may shrink but not grow. A PR that grows it raises LINE_CEILING in its own diff,
which puts the growth in front of a reviewer.

Run: uv run pytest scripts/renderer/tests/test_renderer_size_ceiling.py
"""

import subprocess

from lib.repo_paths import REPO

# 13652 once #7586 split the soak Job's reads into soak_reads.py and retried a transient API error.
# 13657 once #7485 gave the -snapshot and -prepull Kustomizations the default deletion policy.
# 13656 once #7624 shrank the restart-target code by a line.
# 13654 once H1.9 moved the k3s service list to cluster/services.yml.
# 13676 once #7804 made the soak Job count native-sidecar restarts.
# 13680 once #7845 gave the k8s play context the two names its inventory values read.
# 13679 once the Pi stopped being a rollout canary and the dialect dropped `updater_lag_grace_s`.
# 13672 once #7961 dropped the deployer-state modules from the host source tables.
# 13618 once H7.55 dropped the `role:` sources, which the components now own.
# 13622 once #8023 listed the Flux GitHub App's three keys in `SOPS_MANIFEST_KEYS`.
# 13619 once #8172 rewrote the renderer docstrings that cited the deleted deploy.yml.
LINE_CEILING = 13619
# Exit 6's command, `git ls-files 'scripts/renderer/*.py' | grep -v /tests/ | xargs cat | wc -l`.
PATHSPEC = "scripts/renderer/*.py"


def tracked() -> list[str]:
    return subprocess.run(
        ["git", "ls-files", PATHSPEC],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()


def over_ceiling(names: list[str], ceiling: int) -> str | None:
    """The refusal when the non-test files among `names` hold more than `ceiling` lines."""
    lines = sum(
        (REPO / name).read_bytes().count(b"\n")
        for name in names
        if "/tests/" not in name
    )
    if lines <= ceiling:
        return None
    return f"the renderer reads {lines} lines against the ceiling of {ceiling}"


def test_the_renderer_stays_at_or_under_its_ceiling() -> None:
    names = tracked()
    assert len(names) > 40, (
        "the pathspec matched too few files for the count to mean anything"
    )
    assert over_ceiling(names, LINE_CEILING) is None


def test_one_line_over_the_ceiling_is_refused() -> None:
    refusal = over_ceiling(tracked(), LINE_CEILING - 1)
    assert (
        refusal
        == f"the renderer reads {LINE_CEILING} lines against the ceiling of {LINE_CEILING - 1}"
    )
