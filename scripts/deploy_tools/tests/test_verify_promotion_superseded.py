"""A verdict on T counts only while `prod` names T's digest (slice D7.2), as clean/flagged pairs.

`verify` reads `prod`'s digest each poll and before a `failed` verdict, and prints
`superseded` once a later run has moved `prod`. `land-wait` keeps waiting on a soak failure
read after `homelab` moved past R. docs/target-architecture/deploy/d7-superseding-promotions.md
has the design.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion_superseded.py
"""

import forge
from _land_wait_fakes import D, M, at
from deploy_tools.promote_lib import verify_promotion as vp
from test_promote_land_wait import _soak_sim
from test_verify_promotion import CHANGED, PRIOR, PROD, T, Clock, ok

DIGEST = "sha256:" + "1a" * 32
LATER = "sha256:" + "2b" * 32


def reads(states: dict[str, str], prod: list, clock: Clock | None = None):
    """Status reads of T answering `states`, and `prod`'s digest, one answer per read."""
    clock = clock or Clock()
    answers = iter(prod)
    last = [prod[-1]]

    def read_prod():
        answer = next(answers, last[0])
        if isinstance(answer, Exception):
            raise answer
        return answer

    return vp.StatusReads(
        lambda sha, since: dict(states) if sha == T else {},
        clock.sleep,
        clock,
        prod=read_prod,
    )


EXPECTED = vp.expected_set(CHANGED, PROD)
GOOD = ok("homelab", "bento-pdf", "bento-pdf-deferred")
FAILING = {**ok("homelab", "bento-pdf-deferred"), "flux/bento-pdf": "failure"}


def test_t_still_named_by_prod_reads_good_is_clean():
    verdict = vp.verify(reads(GOOD, [DIGEST]), T, PRIOR, EXPECTED, digest=DIGEST)
    assert verdict == vp.Verdict("good")


def test_prod_moved_past_t_reads_superseded_before_the_statuses_is_flagged():
    verdict = vp.verify(reads(GOOD, [LATER]), T, PRIOR, EXPECTED, digest=DIGEST)
    assert verdict.verdict == "superseded"


def test_a_failure_while_prod_names_t_is_failed_is_clean():
    verdict = vp.verify(reads(FAILING, [DIGEST]), T, PRIOR, EXPECTED, digest=DIGEST)
    assert (verdict.verdict, verdict.failed) == ("failed", ("bento-pdf",))


def test_a_failure_read_after_prod_moved_is_superseded_is_flagged():
    """The superseded soak posts `failure` on T at once, so the move is read again first."""
    verdict = vp.verify(
        reads(FAILING, [DIGEST, LATER]), T, PRIOR, EXPECTED, digest=DIGEST
    )
    assert verdict.verdict == "superseded"


def test_a_timeout_after_prod_moved_is_superseded():
    clock = Clock()
    states = ok("homelab")
    # One read per poll up to the timeout, then the move, read just before `failed`.
    prod = [DIGEST] * (EXPECTED.timeout_s + 1) + [LATER]
    verdict = vp.verify(
        reads(states, prod, clock), T, PRIOR, EXPECTED, digest=DIGEST, poll_s=1
    )
    assert verdict.verdict == "superseded"
    assert clock.now >= EXPECTED.timeout_s


def test_an_unreadable_prod_never_reads_as_superseded():
    verdict = vp.verify(
        reads(GOOD, [forge.ForgeError("HTTP 503")]), T, PRIOR, EXPECTED, digest=DIGEST
    )
    assert verdict == vp.Verdict("good")


def test_no_digest_reads_no_prod():
    """A run with no `--digest` behaves as before D7.2."""
    verdict = vp.verify(reads(GOOD, [LATER]), T, PRIOR, EXPECTED)
    assert verdict == vp.Verdict("good")


def test_superseded_exits_0_and_writes_the_verdict(tmp_path):
    out = tmp_path / "out"
    assert vp.report(T, vp.Verdict("superseded"), str(out)) == 0
    assert "verdict=superseded" in out.read_text()


# -- land-wait --------------------------------------------------------------------------------


def test_a_soak_failure_on_r_with_homelab_still_at_r_is_soak_failed_is_clean():
    sim = _soak_sim(soak={M: ["failure"]})
    result = sim.wait()
    assert (result.verdict, result.cause) == ("deploy-failed", "soak-failed")


def test_a_soak_failure_on_r_after_homelab_moved_past_r_keeps_waiting_is_flagged():
    sim = _soak_sim(soak={M: ["failure"], D: ["success"]})
    sim.polls = [at(M, "bento-pdf"), at(D, "bento-pdf")]
    result = sim.wait()
    assert result.verdict == "settled" and result.revision == D
