"""The `promote` decision `decide` returns, and the half of it that runs once T is chosen.

`promote.py:decide` walks master and picks T. `_promotion` then pulls the bundles `verify`
reuses, defers inside the reboot window, names the forward-only tags and snapshot roles T
changes against `last-good`, and records `prod`'s statuses for `verify`'s excuse.
`hand_off` writes what `verify` and `hold` read under `--bundle-dir`. They moved here from
`promote.py` when it reached its length cap (slice D7.5), and `promote.py` re-exports
`Decision` and `hand_off`.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_promote_decide_verification.py`
"""

import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path

# `promote_lib` imports as `deploy_tools.promote_lib`, from scripts/.
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy_tools.promote_lib import forward_only, reboot_window, stateful
from deploy_tools.promote_lib import verify_promotion as verify
from deploy_tools.promote_lib.prune import ReadError
from deploy_tools.promote_lib.reads import Reads


@dataclass(frozen=True)
class Decision:
    """What `decide` concluded.

    Attributes:
        verdict: `promote`, `held`, `current` or `error`.
        target: T for `promote`, empty otherwise.
        digest: the digest of `sha-<T>` for `promote`, empty otherwise.
        reason: why, for every verdict but `promote`.
        prior: `prod`'s commit before the move, empty on the first run.
        prior_digest: `prod`'s digest before the move, empty on the first run.
        reset_digest: what a failed verification resets `prod` to: `last-good`'s digest,
            or `prod`'s when `seed` tags it `last-good` first. Empty on the first run.
        seed: `last-good` is absent, so the `tag` job tags `prod`'s digest with it first.
        clears_hold: the revert rule chose T, so a good verdict sets DEPLOY_HOLD to `none`.
        no_reset: the forward-only tags T changes against `last-good`. When any is named, a
            failed verification writes DEPLOY_HOLD as T's SHA alone and leaves `prod` on T.
        stateful: the snapshot roles T changes against `last-good`. When any is named, a
            failed verification writes DEPLOY_HOLD as `<T> stateful=<names>` and leaves
            `prod` on T, whatever `no_reset` says.
        stateful_record: what the hold's post prints (`stateful.record`).
        hold: what `decide` writes to DEPLOY_HOLD itself: T's SHA on a `held` from the
            forward-only gate or the backstop, `<T> suspended-changed=<tags>` on one from the
            suspended-tag check, else empty.
        bundles: `prod`'s and T's bundle files by `prod` and `target`, for `--bundle-dir`.
        recorded: the newest status on `prod`'s commit for each name in T's changed set, by
            name, for `--bundle-dir`. Empty when the set holds no dependent.
    """

    verdict: str
    target: str = ""
    digest: str = ""
    reason: str = ""
    prior: str = ""
    prior_digest: str = ""
    reset_digest: str = ""
    seed: bool = False
    clears_hold: bool = False
    no_reset: tuple[str, ...] = ()
    stateful: tuple[str, ...] = ()
    stateful_record: dict = field(default_factory=dict)
    hold: str = ""
    bundles: dict[str, dict[str, bytes] | None] = field(default_factory=dict)
    recorded: dict[str, str] = field(default_factory=dict)


def _promotion(
    reads: Reads,
    target: str,
    digest: str,
    prod: str | None,
    prod_digest: str | None,
    last_good: str | None,
    lower: str | None,
    held: tuple | None,
    target_bundle: dict[str, bytes] | None,
    prod_bundle: dict[str, bytes] | None,
    last_good_bundle: dict[str, bytes] | None = None,
) -> Decision:
    """The `promote` decision for T, with the two bundle pulls `verify` reuses.

    Both pulls happen before `prod` moves, so a failed pull promotes nothing. A bundle the
    object-loss check already pulled is reused rather than pulled again. It also names the
    forward-only tags T changes against `last-good` (`forward_only.no_reset_names`), which
    reads `last-good`'s bundle only when `last-good` and `prod` name different digests. And it
    records `prod`'s statuses for `verify`'s excuse (`verify.prior_statuses`), before the move.
    And it names the snapshot roles T changes against the same bundle (`stateful.record`).

    Raises:
        ReadError: T's bundle, or `prod`'s, vanished between its HEAD and its pull, or a
            snapshot Job in T's bundle is unreadable.
        forge.ForgeError: the read of `prod`'s statuses failed.
    """
    target_bundle = target_bundle or _pull(reads, digest, f"sha-{target}")
    if prod_bundle is None and prod_digest is not None:
        prod_bundle = _pull(reads, prod_digest, "prod's bundle")
    # After every other read, so no wait separates the clock from the move (D5.2b2).
    if why := reboot_window.deferral(
        reads.reboot,
        lambda: verify.expected_set(target_bundle, prod_bundle).timeout_s,
        reads.now,
    ):
        return Decision("deferred", reason=f"{target} is {why}")
    seed = last_good is None and prod_digest is not None
    base_bundle = prod_bundle
    if last_good is not None and last_good != prod_digest:
        base_bundle = last_good_bundle or _pull(reads, last_good, "last-good's bundle")
    # A first run has no `last-good`, which therefore lacks every tag T carries.
    base = verify.bundle_digests(base_bundle) if base_bundle is not None else {}
    flagged = reads.forward_only(target)
    if lower is not None:
        flagged |= reads.forward_only(lower)
    target_digests = verify.bundle_digests(target_bundle)
    no_reset = forward_only.no_reset_names(
        forward_only.changed_tags(target_digests, base), flagged
    )
    record = stateful.record(
        target_bundle,
        target_digests,
        verify.bundle_digests(base_bundle) if base_bundle is not None else None,
        digest,
    )
    recorded: dict[str, str] = {}
    if prod is not None and prod_bundle is not None:
        recorded = verify.prior_statuses(
            target_bundle, prod_bundle, lambda: reads.statuses(prod)
        )
        if recorded:
            states = ", ".join(f"flux/{n}={s}" for n, s in sorted(recorded.items()))
            print(
                f"promote.py: recorded on prod's commit {prod}: {states}",
                file=_sys.stderr,
            )
    return Decision(
        "promote",
        target=target,
        digest=digest,
        prior=prod or "",
        prior_digest=prod_digest or "",
        reset_digest=last_good or prod_digest or "",
        seed=seed,
        clears_hold=held is not None,
        no_reset=tuple(sorted(no_reset)),
        stateful=tuple(record["stateful"]),
        stateful_record=record,
        bundles={"prod": prod_bundle, "target": target_bundle},
        recorded=recorded,
    )


def _pull(reads: Reads, digest: str, what: str) -> dict[str, bytes]:
    """The files of the bundle at `digest`.

    Raises:
        ReadError: the registry no longer holds it, so it vanished after its HEAD.
    """
    files = reads.bundle(digest)
    if files is None:
        raise ReadError(f"{what} at {digest} vanished before its pull")
    return files


def hand_off(decision: Decision, bundle_dir: Path | None) -> Decision:
    """Write a `promote` decision's two bundles and its stateful record under `bundle_dir`.

    `verify` reads the bundles, and `promote.py hold` reads the record.

    A write that fails turns the decision into `error`, so `prod` never moves without them.
    """
    if decision.verdict != "promote" or bundle_dir is None:
        return decision
    try:
        target = decision.bundles.get("target")
        if target is None:
            raise ValueError("decide pulled no bundle for T")
        verify.write_handoff(
            bundle_dir, decision.bundles.get("prod"), target, decision.recorded
        )
        stateful.write(bundle_dir, decision.stateful_record)
    except (OSError, ValueError) as err:
        return Decision("error", reason=f"--bundle-dir {bundle_dir}: {err}")
    return decision
