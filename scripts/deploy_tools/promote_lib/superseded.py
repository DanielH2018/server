"""`verify` after a later run moved `prod` past T: the superseded verdict (slice D7.2).

A verdict on T counts only while `prod` still names T's digest. Once a later run has moved
`prod`, that run owns the cluster and its verification covers T's changes too, so `verify`
prints `superseded`, exits 0, and `settle` writes nothing. Each poll reads `prod`'s digest
before T's statuses, and once more before a `failed` verdict, because a soak that a later
revision superseded posts `failure` on T at once.

A failed read of `prod` keeps the wait going and never reads as superseded, so an unreadable
registry cannot end a verification. A run is superseded only while `promote.SUPERSEDE` is
on. docs/archive/deploy/d7-superseding-promotions.md has the design.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/promote/test_verify_promotion_superseded.py`
"""

from collections.abc import Callable

from deploy_tools import forge


def moved_past(read_prod: Callable[[], str | None] | None, digest: str) -> str | None:
    """`prod`'s digest, or `absent`, when it no longer names `digest`; else None.

    None also when there is no `read_prod`, when `digest` is empty, and when the read
    fails, each of which leaves the verification running.

    Args:
        read_prod: reads `prod`'s digest, None when the tag is absent, raising
            `forge.ForgeError` on a failed read.
        digest: T's digest.
    """
    if read_prod is None or not digest:
        return None
    try:
        live = read_prod()
    except forge.ForgeError as err:
        print(f"  verify: prod's digest could not be read: {err}", flush=True)
        return None
    if live == digest:
        return None
    print(
        f"  verify: prod is at {live or 'absent'}, not T's {digest}: a later run moved "
        "it, so this verification is superseded",
        flush=True,
    )
    return live or "absent"
