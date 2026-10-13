"""The determinism check both `test_renderer_determinism_*` modules share.

R1's exit criterion 2: a render reads only the repo and gives the same bytes twice. Each module
takes the tags at its positions of the sorted `render_targets` list and renders them twice,
each time in a subprocess with its own `PYTHONHASHSEED`. The subprocess is
`_render_audit_child.py`, which installs an audit hook before it imports the renderer. The hook
ends the process on any `open` outside the repo, the output directory, the stub file and the
interpreter's own prefixes, and on any `socket.connect`.

When `GITHUB_STEP_SUMMARY` is set, `check_half` appends one digest line per rendered directory
to the job summary, as `shadow_diff.py digest` prints it. A line depends on its directory's
files alone, so the two modules' summaries together hold the full set, whichever shards they
ran on. Neither writes the `total` line, which only a full render can compute.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_even.py \\
         scripts/renderer/tests/test_renderer_determinism_odd.py
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
# A half render takes about 5s on daniel-box. The bound only stops a wedged child.
RENDER_TIMEOUT = 300
# Each half holds 29 tags at 58 render targets (2026-10-13).
HALF_FLOOR = 25


def half_tags(position: int, of: int = 2) -> list[str]:
    """The render targets at `position`, `position + of`, ... of the sorted list."""
    return sorted(render_targets(K8S_HOST))[position::of]


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


def check_half(position: int, tmp_path: Path, module: str) -> None:
    """Render one half twice under the audit hook; assert both trees match, then digest them."""
    tags = half_tags(position)
    assert len(tags) >= HALF_FLOOR, f"only {len(tags)} tags at position {position}"
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

    write_summary(
        module, [digest_line(n, h) for n, h in directory_digests(outs[0]).items()]
    )
