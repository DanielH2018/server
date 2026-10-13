"""R1's determinism check over the render targets at odd positions.

`test_renderer_determinism_even.py` holds the even half and the check's red-proofs.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_odd.py
"""

from _render_audit import check_half


def test_odd_positions_render_identically_and_read_only_the_repo(tmp_path):
    check_half(1, tmp_path, "odd positions")
