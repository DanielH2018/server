"""`asset_pins.py`: the census over the real tree, and the verdict against a fake fetcher.

No test here fetches anything — the suite runs under `-p leakguard`. The fetch is what the
script does when run by hand; the census and the comparison are what this covers.

Run: uv run pytest scripts/validate/tests/test_validate_asset_pins.py
"""

import hashlib
import io

import pytest
from validate import asset_pins
from validate.asset_pins import KNOWN_PINS, Pin, check_pin, discover_pins, pins_in

PAYLOAD = b"plugin bytes"
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()


def _fetcher(status: int = 200, body: bytes = PAYLOAD):
    return lambda url: (status, iter([body[:5], body[5:]]))


def _pin(
    algorithm: str = "sha256", digest: str = SHA256, error: str | None = None
) -> Pin:
    return Pin(
        "x", "https://example.invalid/x.zip", algorithm, digest, "defaults", error
    )


# ── the census ──────────────────────────────────────────────────────────────────────────────


def test_the_census_finds_every_known_pin():
    """The scanner keys on `_url`/`_sha256`/`_md5` names; a rename must fail here, not vanish."""
    pins, _ = discover_pins()
    missing = KNOWN_PINS - {pin.name for pin in pins}
    assert not missing, f"census lost {sorted(missing)}"
    assert len(pins) >= len(KNOWN_PINS)


def test_every_discovered_pin_renders_to_a_fetchable_url():
    pins, _ = discover_pins()
    unresolved = [pin.name for pin in pins if pin.error]
    assert unresolved == [], unresolved
    assert all(pin.url.startswith("https://") and "{{" not in pin.url for pin in pins)


def test_a_flat_pair_with_a_templated_url_is_rendered_from_its_own_file():
    defaults = {
        "tool_version": "v1.2",
        "tool_url": "https://host/{{ tool_version }}/install.sh",
        "tool_sha256": SHA256.upper(),
    }
    pins, unpaired = pins_in(defaults, "d")
    assert [(p.name, p.url, p.algorithm, p.digest) for p in pins] == [
        ("tool", "https://host/v1.2/install.sh", "sha256", SHA256)
    ]
    assert unpaired == []


def test_a_list_of_mods_yields_one_pin_per_item():
    defaults = {
        "mods": [
            {"name": "A", "url": "https://host/a", "sha256": SHA256},
            {"name": "B", "url": "https://host/b", "md5": MD5},
            {"name": "no-digest", "url": "https://host/c"},
        ]
    }
    pins, _ = pins_in(defaults, "d")
    assert [(p.name, p.algorithm) for p in pins] == [
        ("mods[A]", "sha256"),
        ("mods[B]", "md5"),
    ]


def test_a_url_that_does_not_resolve_is_reported_not_skipped():
    defaults = {"tool_url": "https://host/{{ missing }}/x", "tool_sha256": SHA256}
    pins, _ = pins_in(defaults, "d")
    assert len(pins) == 1 and pins[0].error and "missing" in pins[0].error
    assert check_pin(pins[0], _fetcher()) == pins[0].error


def test_a_digest_with_no_url_beside_it_is_listed_as_unpaired():
    defaults = {"exporter_sha256": SHA256, "other_url": "https://host/x"}
    pins, unpaired = pins_in(defaults, "d")
    assert pins == []
    assert unpaired == ["exporter_sha256"]


# ── the verdict ─────────────────────────────────────────────────────────────────────────────


def test_a_matching_download_is_clean():
    assert check_pin(_pin(), _fetcher()) is None
    assert check_pin(_pin("md5", MD5), _fetcher()) is None


def test_a_checksum_mismatch_is_flagged():
    reason = check_pin(_pin(digest="0" * 64), _fetcher())
    assert reason and "does not match" in reason and SHA256 in reason


def test_a_non_200_status_is_flagged_before_hashing():
    reason = check_pin(_pin(), _fetcher(status=404))
    assert reason == "HTTP 404 (must be 200)"


def test_a_fetch_error_is_flagged():
    def broken(url):
        raise OSError("connection refused")

    reason = check_pin(_pin(), broken)
    assert reason and "connection refused" in reason


# ── the cli ─────────────────────────────────────────────────────────────────────────────────


def _main(argv, pins, known=frozenset({"x"})):
    out = io.StringIO()
    code = asset_pins.main(
        argv, fetcher=_fetcher(), out=out, discover=lambda: (pins, []), known=known
    )
    return code, out.getvalue()


def test_main_exits_one_and_names_the_failing_pin():
    code, out = _main([], [_pin(digest="0" * 64)])
    assert code == 1
    assert "FAIL  x" in out


def test_main_refuses_when_the_census_lost_a_known_pin():
    code, out = _main(["--list"], [_pin()], known=frozenset({"x", "renamed_away"}))
    assert code == 1
    assert "renamed_away" in out


@pytest.mark.parametrize("argv", [["--only", "nope"], ["--only", "x,nope"]])
def test_an_unknown_only_name_is_a_usage_error(argv):
    assert _main(argv, [_pin()])[0] == 64
