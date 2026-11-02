"""The bridge stays small: every `.py` under `files/` sums to at most `CAP_LINES` lines.

M6 (docs/target-architecture/monitoring/m6-retire-kuma.md, *The residual bridge and its cap*)
shrinks monitor-bridge to the checks Prometheus cannot express, and a cap holds it there. A
body that wants to join replaces one, or raises the cap in a reviewed PR.
"""

from pathlib import Path

FILES = Path(__file__).resolve().parents[1] / "files"

# Measured at M6 batch A6-8: 7,836 physical lines over 39 modules, deploy_pipeline.py included.
# Rounded up to the next 500, which is how the plan sets it.
CAP_LINES = 8000


def _lines(files: list[Path]) -> int:
    return sum(len(f.read_text().splitlines()) for f in files)


def test_the_bridge_sums_to_no_more_than_its_cap():
    modules = sorted(FILES.rglob("*.py"))
    # Non-vacuity by name: a glob that found nothing would sum to 0 and pass.
    assert {"cli.py", "check.py", "deploy_pipeline.py"} <= {m.name for m in modules}
    total = _lines(modules)
    assert total <= CAP_LINES, (
        f"the bridge's files/ hold {total} lines, over the cap of {CAP_LINES}: replace a body "
        "or raise the cap in a reviewed PR"
    )


def test_a_tree_over_the_cap_is_flagged(tmp_path):
    """The red half: the sum counts every line of every module, so a long module trips it."""
    long_module = tmp_path / "long.py"
    long_module.write_text("x = 1\n" * (CAP_LINES + 1))
    assert _lines([long_module]) > CAP_LINES
