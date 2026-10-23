"""The files `decide` hands `verify` under `--bundle-dir`, read back as `decide` wrote them.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_handoff.py
"""

from test_verify_promotion import CHANGED, PROD

from deploy_tools.promote_lib import verify_handoff
from deploy_tools.promote_lib import verify_promotion as vp


def test_the_handoff_carries_digests_and_flux_files_and_nothing_else(tmp_path):
    verify_handoff.write_handoff(
        tmp_path, {**PROD, "cluster/bento-pdf/x.yaml": b"x"}, CHANGED
    )
    prod, target = verify_handoff.read_handoff(tmp_path)
    assert prod == PROD and target == CHANGED


def test_the_handoff_carries_the_record_beside_the_bundles(tmp_path):
    verify_handoff.write_handoff(tmp_path, PROD, CHANGED, {"longhorn": "failure"})
    assert verify_handoff.read_handoff(tmp_path) == (PROD, CHANGED)
    assert verify_handoff.read_recorded(tmp_path) == {"longhorn": "failure"}
    (tmp_path / verify_handoff.RECORDED_FILE).unlink()
    assert verify_handoff.read_recorded(tmp_path) == {}, "no record excuses nothing"


def test_a_first_run_hands_on_no_prod(tmp_path):
    verify_handoff.write_handoff(tmp_path, None, CHANGED)
    assert verify_handoff.read_handoff(tmp_path) == (None, CHANGED)
    assert vp.expected_set(CHANGED, None).widened
