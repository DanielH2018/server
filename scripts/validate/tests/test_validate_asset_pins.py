"""`asset_pins.py`: the census over the real tree, and the verdict against a fake fetcher.

No test here fetches anything — the suite runs under `-p leakguard`. The fetch is what the
script does when run by hand; the census and the comparison are what this covers.

Run: uv run pytest scripts/validate/tests/test_validate_asset_pins.py
"""

import dataclasses
import gzip
import hashlib
import io

import pytest
from validate import asset_pins
from validate.asset_pins import (
    KNOWN_PINS,
    Pin,
    check_pin,
    discover_pins,
    pins_in,
    refresh_pin,
)

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


def test_the_census_names_the_annotated_depname_behind_each_pin():
    """`--refresh getsops/sops` selects both per-arch pins through the annotation."""
    by_name = {pin.name: pin for pin in discover_pins()[0]}
    assert by_name["sops_setup_binary_amd64"].dep_names == {"getsops/sops"}
    assert by_name["sops_setup_binary_arm64"].dep_names == {"getsops/sops"}
    assert by_name["k3s_install_script"].dep_names == {"k3s-io/k3s"}


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


def test_a_pin_records_the_keys_its_url_renders_from_through_another_key():
    defaults = {
        "tool_version": "1.2",
        "tool_tag": "v{{ tool_version }}",
        "tool_url": "https://host/{{ tool_tag }}/x",
        "tool_sha256": SHA256,
    }
    pins, _ = pins_in(defaults, "d")
    assert pins[0].inputs == {"tool_tag", "tool_version"}


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


class _Response(io.BytesIO):
    def __init__(self, body: bytes, encoding: str | None):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Encoding": encoding} if encoding else {}


def _fetch_served(body: bytes, encoding: str | None) -> bytes:
    status, chunks = asset_pins.fetch(
        "https://example.invalid/x.vsix",
        open_url=lambda request, timeout: _Response(body, encoding),
    )
    assert status == 200
    return b"".join(chunks)


def test_fetch_decodes_a_gzip_content_encoding():
    # The marketplace gzips every vspackage; the role pins the decoded file's hash.
    assert _fetch_served(gzip.compress(PAYLOAD), "gzip") == PAYLOAD


def test_fetch_passes_an_unencoded_body_through():
    assert _fetch_served(PAYLOAD, None) == PAYLOAD


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


# ── --refresh ───────────────────────────────────────────────────────────────────────────────

OLD = "0" * 64
DEFAULTS_TEXT = f"""\
# renovate: datasource=github-releases depName=vendor/tool
tool_version: "v1.3"
tool_url: "https://host/{{{{ tool_version }}}}/tool"
tool_sha256: {OLD}
"""


def _refreshable(tmp_path, text: str = DEFAULTS_TEXT) -> Pin:
    (tmp_path / "defaults.yml").write_text(text)
    return Pin(
        "tool",
        "https://host/v1.3/tool",
        "sha256",
        OLD,
        "defaults.yml",
        dep_names=frozenset({"vendor/tool"}),
    )


def test_refresh_rewrites_the_digest_and_keeps_the_annotation(tmp_path):
    ok, message = refresh_pin(_refreshable(tmp_path), _fetcher(), tmp_path)
    assert ok, message
    assert (tmp_path / "defaults.yml").read_text() == DEFAULTS_TEXT.replace(OLD, SHA256)


def test_refresh_leaves_a_current_digest_alone(tmp_path):
    pin = dataclasses.replace(
        _refreshable(tmp_path, DEFAULTS_TEXT.replace(OLD, SHA256)), digest=SHA256
    )
    ok, message = refresh_pin(pin, _fetcher(), tmp_path)
    assert ok and "already current" in message


def test_refresh_refuses_a_digest_that_occurs_twice(tmp_path):
    pin = _refreshable(tmp_path, DEFAULTS_TEXT + f"other_sha256: {OLD}\n")
    ok, message = refresh_pin(pin, _fetcher(), tmp_path)
    assert not ok and "occurs 2 times" in message
    assert SHA256 not in (tmp_path / "defaults.yml").read_text()


def test_refresh_writes_nothing_when_the_download_fails(tmp_path):
    ok, message = refresh_pin(_refreshable(tmp_path), _fetcher(status=404), tmp_path)
    assert not ok and message == "HTTP 404 (must be 200)"
    assert (tmp_path / "defaults.yml").read_text() == DEFAULTS_TEXT


def test_main_refresh_selects_pins_by_depname(tmp_path):
    pin = _refreshable(tmp_path)
    out = io.StringIO()
    code = asset_pins.main(
        ["--refresh", "vendor/tool"],
        fetcher=_fetcher(),
        out=out,
        discover=lambda: ([pin, _pin()], []),
        known=frozenset({"x"}),
        root=tmp_path,
    )
    assert code == 0, out.getvalue()
    assert "1/1 pin(s) current" in out.getvalue()
    assert SHA256 in (tmp_path / "defaults.yml").read_text()


def test_an_unknown_refresh_name_is_a_usage_error():
    assert _main(["--refresh", "vendor/nope"], [_pin()])[0] == 64
