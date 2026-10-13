"""The determinism check the `test_renderer_determinism_<k>.py` modules share.

R1's exit criterion 2: a render reads only the repo and gives the same bytes twice. Module `k`
takes the tags at positions `k`, `k + PARTS`, ... of the sorted `render_targets` list and
renders them twice, each time in a subprocess with its own `PYTHONHASHSEED`. The subprocess is
`_render_audit_child.py`, which installs an audit hook before it imports the renderer. The hook
ends the process on any `open` outside the repo, the output directory, the stub file and the
interpreter's own prefixes, and on any `socket.connect`.

In CI, `check_part` writes one digest line per rendered directory, as `shadow_diff.py digest`
prints it, to two places. When `GITHUB_STEP_SUMMARY` is set, it appends them to the job
summary, which only a signed-in viewer can read. When `GITHUB_SHA` is set, it also records
them on the test's report, and the `pytest_terminal_summary` hook in this directory's
`conftest.py` prints them to the job log, which `gh run view --log` reads. A line depends on
its directory's files alone, so the modules' lines together hold the full set, whichever
shards they ran on. None writes the `total` line, which only a full render can compute.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_0.py \\
         scripts/renderer/tests/test_renderer_determinism_1.py \\
         scripts/renderer/tests/test_renderer_determinism_2.py \\
         scripts/renderer/tests/test_renderer_determinism_3.py
"""

import json
import os
import sys
from pathlib import Path

from deploy_tools.render_targets import render_targets
from lib.ansible_inventory import K8S_HOST
from lib.proc_testing import run
from lib.repo_paths import REPO, SCRIPTS
from renderer import shadow_diff
from renderer.renderer_lib.shadow import CLUSTER, digest_line, directory_digests

CHILD = Path(__file__).resolve().with_name("_render_audit_child.py")
SEEDS = ("1", "2")
# A part's render takes about 2.5s on daniel-box. The bound only stops a wedged child.
RENDER_TIMEOUT = 300
# One module per position. A module runs about 3.4 times slower on a CI runner than serially
# on daniel-box: the two-module split took 28.0s there against 8.2s here (2026-10-13). Four
# parts keep each under the R1 plan's 20s, where three were projected at 20.3s.
PARTS = 4
# Each part holds 14 or 15 tags at 58 render targets (2026-10-13).
PART_FLOOR = 12
MODULE_PREFIX = "test_renderer_determinism_"
# The report property `check_part` records its digest lines under, for `log_digest_lines`.
DIGEST_PROPERTY = "render_digest"


def part_tags(position: int, of: int = PARTS) -> list[str]:
    """The render targets at `position`, `position + of`, ... of the sorted list."""
    return sorted(render_targets(K8S_HOST))[position::of]


def module_position(module_file: str) -> int:
    """The position a `test_renderer_determinism_<k>.py` module checks, read from its name."""
    return int(Path(module_file).stem.removeprefix(MODULE_PREFIX))


def run_step(
    spec: dict,
    seed: str = SEEDS[0],
    timeout: float = 60,
    ansible_tmp: Path | None = None,
):
    """Run one step of `_render_audit_child.py` under the audit hook, from the repo root.

    `ansible_tmp` becomes `ANSIBLE_LOCAL_TEMP`. ansible-core creates that directory when the
    renderer imports it, and its default is under the invoking user's home.
    """
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(SCRIPTS)}
    if ansible_tmp is not None:
        env["ANSIBLE_LOCAL_TEMP"] = str(ansible_tmp)
    return run(
        [sys.executable, str(CHILD), json.dumps(spec)],
        cwd=REPO,
        env=env,
        timeout=timeout,
    )


def tree_differences(first: Path, second: Path) -> list[str]:
    """Each file that only one tree holds, or that both hold with different bytes."""
    files = [
        {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        for root in (first, second)
    ]
    return sorted(
        [f"only in {first.name}: {rel}" for rel in files[0] - files[1]]
        + [f"only in {second.name}: {rel}" for rel in files[1] - files[0]]
        + [
            f"differs: {rel}"
            for rel in files[0] & files[1]
            if (first / rel).read_bytes() != (second / rel).read_bytes()
        ]
    )


def write_summary(module: str, lines: list[str]) -> None:
    """Append `lines` to the job summary under a heading naming the commit, when in CI."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    sha = os.environ.get("GITHUB_SHA", "unknown")
    with open(summary, "a") as out:
        out.write(f"### render digest at {sha} ({module})\n\n```text\n")
        out.write("".join(f"{line}\n" for line in lines))
        out.write("```\n\n")


def log_digest_lines(terminalreporter) -> None:
    """Print each digest line the session's tests recorded, prefixed `render digest at <sha>`.

    A `pytest_terminal_summary` hook calls this in the process that prints the report. Under
    xdist that is the controller, which runs no test. The lines reach it as the call reports'
    `user_properties`, which xdist carries back from the workers. A worker's own stdout never
    reaches the job log.
    """
    for reports in terminalreporter.stats.values():
        for report in reports:
            if getattr(report, "when", None) != "call":
                continue
            for name, value in report.user_properties:
                if name != DIGEST_PROPERTY:
                    continue
                prefix = f"render digest at {value['sha']} ({value['module']}):"
                for line in value["lines"]:
                    terminalreporter.write_line(f"{prefix} {line}")


def check_part(module_file: str, tmp_path: Path, record_property) -> None:
    """Render one part twice under the audit hook; assert both trees match, then digest them.

    Args:
        module_file: The calling module's `__file__`. Its name gives the part's position.
        tmp_path: The test's own directory, for the stub file and both renders.
        record_property: The test's fixture of that name. It carries the digest lines to
            `log_digest_lines`.
    """
    position = module_position(module_file)
    assert 0 <= position < PARTS, f"{module_file} names no position below {PARTS}"
    module = f"position {position} of {PARTS}"
    tags = part_tags(position)
    assert len(tags) >= PART_FLOOR, f"only {len(tags)} tags at position {position}"
    stubs = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(stubs)]) == 0

    outs = []
    for seed in SEEDS:
        out, ansible_tmp = tmp_path / f"seed-{seed}", tmp_path / f"ansible-tmp-{seed}"
        spec = {
            "allow": [str(REPO), str(out), str(stubs), str(ansible_tmp)],
            "step": "render",
            "out": str(out),
            "tags": tags,
            "stubs": str(stubs),
        }
        done = run_step(spec, seed, RENDER_TIMEOUT, ansible_tmp)
        assert done.returncode == 0, f"seed {seed}: {done.stderr}"
        outs.append(out)

    rendered = {p.name for p in (outs[0] / CLUSTER).iterdir()}
    assert set(tags) <= rendered, f"never rendered {sorted(set(tags) - rendered)}"
    assert not (differences := tree_differences(*outs)), "\n".join(differences)

    lines = [digest_line(n, h) for n, h in directory_digests(outs[0]).items()]
    write_summary(module, lines)
    if sha := os.environ.get("GITHUB_SHA"):
        record_property(DIGEST_PROPERTY, {"sha": sha, "module": module, "lines": lines})
