"""Property tests for the R2 etcd lock canary's plan, which picks the keys the bridge deletes."""

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from verdicts.storage import R2_CANARY_PREFIX, r2_canary_plan

# Far enough inside `date`'s range that no offset or lock age below overflows.
TODAYS = st.dates(min_value=date(1, 6, 1), max_value=date(9999, 12, 20))
LOCK_DAYS = st.integers(1, 60)


def _spellings(day: date) -> list[str]:
    """Names `date.fromisoformat` reads as `day`, or nearly so, that no canary carries."""
    year, week, weekday = day.isocalendar()
    return [
        day.isoformat().replace("-", ""),
        "%04d-W%02d-%d" % (year, week, weekday),
        "%04dW%02d%d" % (year, week, weekday),
        day.isoformat() + ".bak",
        day.isoformat() + "T00:00:00",
        day.isoformat() + "/",
    ]


@st.composite
def listings(draw) -> tuple[date, list[str], list[date]]:
    """(today, listed keys, the days that have a canary among them), duplicates included."""
    today = draw(TODAYS)
    days = st.integers(-90, 5).map(lambda n: today + timedelta(days=n))
    canaries = draw(st.lists(days, max_size=12))
    junk = draw(
        st.lists(
            st.one_of(
                days.flatmap(lambda d: st.sampled_from(_spellings(d))),
                st.sampled_from(["", "not-a-date", "2026-13-01"]),
                st.text(max_size=12),
            ),
            max_size=6,
        )
    )
    # `st.text` can spell a canary's own date, which would make it a canary.
    names = {d.isoformat() for d in (today + timedelta(n) for n in range(-90, 6))}
    keys = [R2_CANARY_PREFIX + d.isoformat() for d in canaries]
    keys += [R2_CANARY_PREFIX + name for name in junk if name not in names]
    return today, draw(st.permutations(keys)), canaries


def _canary(day: date) -> str:
    return R2_CANARY_PREFIX + day.isoformat()


@given(listings(), LOCK_DAYS)
def test_expired_holds_exactly_the_canaries_past_the_lock_by_a_day(listing, lock_days):
    today, keys, canaries = listing
    plan = r2_canary_plan(keys, today, lock_days)
    cutoff = today - timedelta(days=lock_days + 1)
    assert plan.expired == tuple(
        _canary(d) for d in sorted(set(canaries)) if d <= cutoff
    )
    assert plan.probe not in plan.expired


@given(listings(), LOCK_DAYS)
def test_the_probe_is_the_newest_earlier_canary_inside_the_lock_else_todays(
    listing, lock_days
):
    today, keys, canaries = listing
    oldest = today - timedelta(days=lock_days - 1)
    locked = [d for d in canaries if oldest <= d < today]
    plan = r2_canary_plan(keys, today, lock_days)
    assert plan.probe == _canary(max(locked, default=today))


@given(listings(), LOCK_DAYS)
def test_today_is_uploaded_unless_listed_so_the_probe_exists(listing, lock_days):
    today, keys, _ = listing
    plan = r2_canary_plan(keys, today, lock_days)
    assert plan.upload == (None if _canary(today) in keys else _canary(today))
    # A real R2 delete of an absent key succeeds, which the row reads as a missing lock.
    assert plan.probe in [*keys, plan.upload]
