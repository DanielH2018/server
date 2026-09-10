"""The DRM sysfs GPU exporter (#1446).

The trees built here are copies of what the two nodes actually expose, measured 2026-09-10:
daniel-box is `card1` on `amdgpu` with `gpu_busy_percent` and an hwmon `freq1_input` in Hz;
daniel-server is `card0` on `i915` with no busy percentage at all, `power/rc6_residency_ms`
and `gt_act_freq_mhz`. Every test asserts a rendered VALUE against the file it came from, so a
read path that silently stops working fails here rather than presenting a flat zero.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "files"))

import gpu_exporter


def _card(root, name, driver):
    """Build one DRM card, including the `device/driver` symlink discovery keys off."""
    card = root / "class" / "drm" / name
    (card / "device").mkdir(parents=True)
    driver_dir = root / "bus" / "pci" / "drivers" / driver
    driver_dir.mkdir(parents=True, exist_ok=True)
    (card / "device" / "driver").symlink_to(driver_dir)
    return card


def _connector(root, name, card_name):
    """A DRM connector — matches `card[0-9]*` but is not a card.

    Its `device` points back at the card and it has NO `device/driver` of its own, which is the
    only thing distinguishing it. daniel-box exposes nine of these.
    """
    conn = root / "class" / "drm" / name
    conn.mkdir(parents=True)
    (conn / "device").symlink_to(root / "class" / "drm" / card_name)


def _amd_tree(tmp_path, busy=37, freq_hz=1_100_000_000):
    root = tmp_path / "sys"
    card = _card(root, "card1", "amdgpu")
    (card / "device" / "gpu_busy_percent").write_text("%d\n" % busy)
    hwmon = card / "device" / "hwmon" / "hwmon3"
    hwmon.mkdir(parents=True)
    (hwmon / "freq1_input").write_text("%d\n" % freq_hz)
    for n in ("card1-DP-1", "card1-HDMI-A-1", "card1-Writeback-1"):
        _connector(root, n, "card1")
    return root


def _intel_tree(tmp_path, rc6_ms=3_418_772, freq_mhz=650):
    root = tmp_path / "sys"
    card = _card(root, "card0", "i915")
    (card / "power").mkdir()
    (card / "power" / "rc6_residency_ms").write_text("%d\n" % rc6_ms)
    (card / "gt_act_freq_mhz").write_text("%d\n" % freq_mhz)
    return root


def _by_name(samples):
    return {name: (labels, value) for name, labels, value in samples}


# ── discovery ────────────────────────────────────────────────────────────────────────────


def test_discovery_finds_the_card_and_names_its_driver(tmp_path):
    assert gpu_exporter.discover_cards(_amd_tree(tmp_path)) == [("card1", "amdgpu")]


def test_discovery_rejects_connectors_that_match_the_card_glob(tmp_path):
    """The must-reject half of the pair, and a real regression.

    `Path.resolve()` on a missing path returns it unchanged instead of raising, so resolving
    `device/driver` before proving it exists reported all nine of daniel-box's connectors as
    cards with the driver literally named `"driver"`.
    """
    found = gpu_exporter.discover_cards(_amd_tree(tmp_path))
    assert [name for name, _ in found] == ["card1"]
    assert "driver" not in [driver for _, driver in found]


def test_the_card_number_is_not_assumed(tmp_path):
    """daniel-box is card1 and daniel-server is card0, so nothing may index a fixed number."""
    assert gpu_exporter.discover_cards(_intel_tree(tmp_path)) == [("card0", "i915")]


# ── amdgpu: a direct busy gauge ──────────────────────────────────────────────────────────


def test_amdgpu_busy_percent_is_the_sysfs_value(tmp_path):
    samples = _by_name(gpu_exporter.collect(_amd_tree(tmp_path, busy=37)))
    assert samples[gpu_exporter._BUSY][1] == 37.0
    assert samples[gpu_exporter._BUSY][0] == {"card": "card1", "driver": "amdgpu"}


def test_amdgpu_frequency_is_converted_from_hwmon_hz_to_mhz(tmp_path):
    # daniel-box reported 800000000 Hz, which must not surface as 800000000 MHz.
    samples = _by_name(gpu_exporter.collect(_amd_tree(tmp_path, freq_hz=800_000_000)))
    assert samples[gpu_exporter._FREQ][1] == 800.0


def test_amdgpu_emits_no_rc6_counter(tmp_path):
    # amdgpu has no RC6 residency; emitting 0 would read as a permanently 100%-busy GPU
    # through `1 - rate()`.
    assert gpu_exporter._RC6 not in _by_name(gpu_exporter.collect(_amd_tree(tmp_path)))


# ── i915: RC6 residency, the unprivileged stand-in for a busy gauge ──────────────────────


def test_i915_rc6_residency_is_reported_in_seconds(tmp_path):
    # The file is MILLISECONDS and the series is named _seconds_total. Getting this backwards
    # scales every derived utilization figure by 1000 and still plots a plausible line.
    samples = _by_name(gpu_exporter.collect(_intel_tree(tmp_path, rc6_ms=3_418_772)))
    assert samples[gpu_exporter._RC6][1] == 3418.772


def test_i915_frequency_is_read_in_mhz_without_hwmon(tmp_path):
    samples = _by_name(gpu_exporter.collect(_intel_tree(tmp_path, freq_mhz=650)))
    assert samples[gpu_exporter._FREQ][1] == 650.0


def test_i915_emits_no_busy_percent(tmp_path):
    """i915 exposes none, so a series here would be invented rather than measured."""
    assert gpu_exporter._BUSY not in _by_name(
        gpu_exporter.collect(_intel_tree(tmp_path))
    )


# ── the exposition itself ────────────────────────────────────────────────────────────────


def test_a_node_with_no_gpu_renders_nothing_rather_than_zeroes(tmp_path):
    """The must-reject half for the whole collector.

    An empty exposition is honest; a zero-valued one is indistinguishable from an idle GPU.
    """
    (tmp_path / "sys" / "class" / "drm").mkdir(parents=True)
    assert gpu_exporter.collect(tmp_path / "sys") == []
    assert gpu_exporter.render([]) == "\n"


def test_rc6_is_a_counter_and_busy_is_a_gauge(tmp_path):
    """The TYPE lines are what make `rate()` legal on one and not the other."""
    amd = gpu_exporter.render(gpu_exporter.collect(_amd_tree(tmp_path / "a")))
    intel = gpu_exporter.render(gpu_exporter.collect(_intel_tree(tmp_path / "i")))
    assert "# TYPE %s gauge" % gpu_exporter._BUSY in amd
    assert "# TYPE %s counter" % gpu_exporter._RC6 in intel


def test_a_family_with_no_samples_gets_no_type_line(tmp_path):
    rendered = gpu_exporter.render(gpu_exporter.collect(_intel_tree(tmp_path)))
    assert gpu_exporter._BUSY not in rendered


def test_every_declared_family_is_reachable_from_a_real_tree(tmp_path):
    """Non-vacuity: each name in `_HELP` must be emitted by one of the two node shapes.

    Without this a family renamed in `_HELP` but not at its call site — or the reverse — leaves
    a metric nothing ever writes, and every assertion above still passes.
    """
    rendered = gpu_exporter.render(
        gpu_exporter.collect(_amd_tree(tmp_path / "a"))
        + gpu_exporter.collect(_intel_tree(tmp_path / "i"))
    )
    for name in gpu_exporter._HELP:
        assert "# TYPE %s " % name in rendered, name
    assert set(gpu_exporter._HELP) == {
        gpu_exporter._INFO,
        gpu_exporter._BUSY,
        gpu_exporter._RC6,
        gpu_exporter._FREQ,
    }


def test_an_unreadable_file_costs_one_series_not_the_scrape(tmp_path):
    root = _amd_tree(tmp_path)
    (root / "class" / "drm" / "card1" / "device" / "gpu_busy_percent").unlink()
    samples = _by_name(gpu_exporter.collect(root))
    assert gpu_exporter._BUSY not in samples
    # The card is still announced, which is what separates "reports nothing" from "not read".
    assert samples[gpu_exporter._INFO][1] == 1.0


def test_a_non_numeric_sysfs_value_is_dropped_not_rendered_as_zero(tmp_path):
    root = _amd_tree(tmp_path)
    (root / "class" / "drm" / "card1" / "device" / "gpu_busy_percent").write_text(
        "N/A\n"
    )
    assert gpu_exporter._BUSY not in _by_name(gpu_exporter.collect(root))
