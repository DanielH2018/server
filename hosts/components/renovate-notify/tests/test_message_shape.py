"""What the posted message looks like: the digest's tail survives the cap, and no emojis."""

import notify_logic as nl
from ci_verdict import CIVerdict
import pending_logic as pl
import renovate_notify as rn


def _pr(
    number=1,
    url="u",
    automerge=True,
    ci=CIVerdict.PASS,
    conflicting=False,
    dead_paths=None,
    title="t",
):
    return nl.PR(
        number=number,
        title=title,
        url=url,
        automerge=automerge,
        ci=ci,
        conflicting=conflicting,
        created_at="",
        dead_paths=dead_paths,
    )


def _long_autoclosed(n):
    return nl.render_autoclosed(
        [
            nl.Autoclosed(
                i,
                "chore(deps): update %s - autoclosed" % ("y" * 60),
                "http://x/%d" % i,
                "b%d" % i,
            )
            for i in range(n)
        ]
    )


def test_a_long_prefix_still_ends_with_the_digest():
    items = [
        (
            _pr(number=900, url="http://x/900", automerge=False),
            "manual",
        )
    ]
    msg = nl.compose_message([_long_autoclosed(30)], items)
    assert len(msg) <= nl.DISCORD_MAX
    assert msg.endswith("http://x/900")


def test_a_digest_over_the_floor_behind_a_long_prefix_still_lists_prs():
    url = "https://github.com/owner/repo/pull/%d"
    items = [
        (
            _pr(number=n, url=url % n, title="chore(deps): update %s" % ("z" * 48)),
            "stuck",
        )
        for n in range(1, 6)
    ]
    assert len(nl.render_digest(items)) > nl.DIGEST_FLOOR
    msg = nl.compose_message([_long_autoclosed(20)], items)
    assert len(msg) <= nl.DISCORD_MAX
    assert url % 1 in msg
    assert msg.endswith("more")


def test_a_prefix_that_fits_is_not_cut():
    items = [(_pr(number=5, url="http://x/5", automerge=False), "manual")]
    prefix = _long_autoclosed(2)
    msg = nl.compose_message([prefix], items)
    assert msg == prefix + "\n\n" + nl.render_digest(items)


def test_rendered_messages_carry_no_emoji():
    items = [
        (_pr(number=1, ci=CIVerdict.FAIL), "stuck"),
        (_pr(number=2, conflicting=True), "stuck"),
        (_pr(number=3, conflicting=True, dead_paths=("gone/x",)), "stuck"),
        (_pr(number=4, ci=CIVerdict.PENDING, automerge=False), "manual"),
        (_pr(number=5, automerge=False), "manual"),
    ]
    text = "\n".join(
        [
            nl.compose_message([nl.render_problems({"p"}), _long_autoclosed(1)], items),
            nl.CLEARED_MSG,
            nl.DASHBOARD_UNPARSEABLE_MSG,
            rn.DASHBOARD_STALE_MSG,
            pl.render_pending([("b", "d", 20)]),
            pl.render_churning([("b", "d", 40, 3)]),
            pl.render_pending_reset(0, "o/r"),
        ]
    )
    emoji = [
        c
        for c in text
        if ord(c) >= 0x1F000 or 0x2300 <= ord(c) <= 0x27BF or ord(c) == 0xFE0F
    ]
    assert emoji == []
