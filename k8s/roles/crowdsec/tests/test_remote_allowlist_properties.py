"""Properties of the remote-ips plan and the access-log reader (`files/remote_allowlist.py`).

Sightings, item values and home networks come from one small pool, so the same value turns up
as a sighting, a stored item and a home network in one draw.

Run: uv run pytest k8s/roles/crowdsec/tests/test_remote_allowlist_properties.py
"""

import ipaddress
import json
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

import remote_allowlist as ra

ROUTER = "homelab-app-0123456789abcdef0123@kubernetescrd"

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
nows = st.timedeltas(timedelta(0), timedelta(days=20 * 365)).map(lambda d: EPOCH + d)


def _networks():
    def widen(address, prefix):
        return ipaddress.ip_network(
            f"{address}/{prefix % (address.max_prefixlen + 1)}", strict=False
        )

    return st.builds(widen, st.ip_addresses(), st.integers(0, 128))


@st.composite
def _pool(draw):
    addresses = draw(st.lists(st.ip_addresses(), min_size=1, max_size=4))
    siblings = [
        a + offset
        for a in addresses
        if a.version == 6 and int(a) < 2**128 - 2**16
        for offset in draw(st.lists(st.integers(1, 2**16), max_size=2))
    ]
    singles = [str(a) for a in addresses + siblings]
    sixty_fours = [ra.entry_value(v) for v in singles if ":" in v]
    junk = draw(st.lists(st.text(max_size=6), max_size=2))
    return singles + sixty_fours + junk


def _expirations(now):
    offsets = [timedelta(0), ra.REFRESH_BELOW, ra.TTL]
    exact = [
        now + o + d
        for o in offsets
        for d in (-timedelta(microseconds=1), timedelta(0), timedelta(microseconds=1))
    ]
    stamps = st.one_of(
        st.sampled_from(exact),
        st.timedeltas(-2 * ra.TTL, 2 * ra.TTL).map(lambda d: now + d),
    )
    return st.one_of(
        stamps.map(lambda t: t.isoformat().replace("+00:00", "Z")),
        stamps.map(lambda t: t.replace(tzinfo=None).isoformat()),
        st.sampled_from(["0001-01-01T00:00:00Z", "soon", "", None]),
        st.integers(),
        st.text(max_size=8),
    )


@st.composite
def worlds(draw):
    now = draw(nows)
    pool = draw(_pool())
    values = st.sampled_from(pool)
    items = draw(
        st.lists(
            st.fixed_dictionaries(
                {"value": st.one_of(values, values, st.none(), st.integers())},
                optional={"expiration": _expirations(now)},
            ),
            max_size=8,
        )
    )
    seen = frozenset(
        v for v in draw(st.lists(values, max_size=6)) if ra.is_remote_client(v)
    )
    home = frozenset(
        draw(
            st.lists(
                st.one_of(
                    values.map(lambda v: ra.home_networks([{"value": v}])),
                    _networks().map(lambda n: frozenset({n})),
                ),
                max_size=2,
            )
        )
    )
    home = frozenset().union(*home)
    cap = draw(st.integers(0, 4))
    return seen, items, now, home, cap


def _plan(world):
    seen, items, now, home, cap = world
    return ra.plan(seen, items, now, home=home, cap=cap)


def _unexpired_present(items, now):
    return {
        item["value"]
        for item in items
        if isinstance(item["value"], str)
        and item["value"]
        and (
            (expires := ra.parse_expiration(item.get("expiration"))) is None
            or expires > now
        )
    }


def _apply(items, decided, now):
    """`main`'s writes: remove every copy of each prune and refresh value, then add with full TTL."""
    removed = set(decided.prune) | set(decided.refresh)
    kept = [item for item in items if item["value"] not in removed]
    stamp = (now + ra.TTL).isoformat().replace("+00:00", "Z")
    return kept + [
        {"value": v, "expiration": stamp} for v in decided.refresh + decided.add
    ]


@given(worlds())
def test_every_seen_address_outside_home_lands_in_exactly_one_bucket(world):
    seen, _, _, home, _ = world
    decided = _plan(world)
    fresh = set(decided.live) - set(decided.add) - set(decided.refresh)
    for value in {ra.entry_value(v) for v in seen if not ra.is_home_covered(v, home)}:
        buckets = [
            value in decided.add,
            value in decided.refresh,
            value in decided.over_cap,
            value in fresh,
        ]
        assert buckets.count(True) == 1, value


@given(worlds())
def test_add_holds_no_value_already_unexpired_and_nothing_is_both_pruned_and_live(
    world,
):
    _, items, now, _, _ = world
    decided = _plan(world)
    assert not set(decided.add) & _unexpired_present(items, now)
    assert not set(decided.prune) & set(decided.live)
    assert len(decided.live) == len(set(decided.live))


@given(worlds())
def test_no_home_covered_value_is_live_added_or_refreshed(world):
    _, _, _, home, _ = world
    decided = _plan(world)
    assert not [
        v
        for v in decided.live + decided.add + decided.refresh
        if ra.is_home_covered(v, home)
    ]


@given(worlds())
def test_the_cap_holds(world):
    _, items, now, _, cap = world
    decided = _plan(world)
    assert len(decided.live) <= max(cap, len(_unexpired_present(items, now)))


@given(worlds())
def test_applying_the_plan_and_planning_again_writes_nothing(world):
    seen, items, now, home, cap = world
    decided = _plan(world)
    again = ra.plan(seen, _apply(items, decided, now), now, home=home, cap=cap)
    assert (again.add, again.prune, again.refresh) == ([], [], [])
    assert again.live == decided.live


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=4), children, max_size=3)
    ),
    max_leaves=8,
)
log_entries = st.fixed_dictionaries(
    {},
    optional={
        "RouterName": st.one_of(st.just(ROUTER), json_values),
        "DownstreamStatus": st.one_of(st.sampled_from([200, 304, 302]), json_values),
        "ClientHost": st.one_of(st.ip_addresses().map(str), json_values),
    },
)
log_lines = st.one_of(
    log_entries.map(json.dumps),
    json_values.map(json.dumps),
    st.text(max_size=20),
)


@given(st.lists(log_lines, max_size=6))
def test_authenticated_clients_never_raises_and_returns_only_remote_addresses(lines):
    clients = ra.authenticated_clients(lines, frozenset({ROUTER}))
    assert all(ra.is_remote_client(host) for host in clients)
