"""R1's determinism check over the render targets at position 1.

`test_renderer_determinism_0.py` holds position 0 and the check's red-proofs.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_1.py
"""

from _render_audit import check_part


def test_its_positions_render_identically_and_read_only_the_repo(
    tmp_path, record_property
):
    check_part(__file__, tmp_path, record_property)
