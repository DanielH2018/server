"""GPU utilization as Prometheus series, read from the kernel's DRM sysfs.

Transcode load on the render node was unmeasured until this existed (#1446): the
dri-device-plugin advertises `/dev/dri/renderD128` as `devic.es/dri`, jellyfin and tdarr both
request it, and nothing turned the resulting load into a series. A slow transcode and a
contended one looked identical.

THE TWO NODES HAVE DIFFERENT GPUs, AND THE ISSUE NAMED THE WRONG ONE. Measured 2026-09-10:
daniel-box is an AMD Phoenix3 on `amdgpu` (`card1`), daniel-server an Intel Iris Xe on `i915`
(`card0`). Both advertise `devic.es/dri`, and jellyfin and tdarr were both scheduled on
daniel-box — the AMD one — so an `intel_gpu_top`-based exporter would have instrumented the
node where no transcoding happens. This reads sysfs and branches on the driver, so it follows
the pods wherever they schedule.

THE TWO DRIVERS DO NOT EXPOSE THE SAME THING, so the utilization series differ by driver:

  amdgpu  `gpu_busy_percent` is a direct 0-100 gauge. Use it as-is.
  i915    exposes no busy percentage in sysfs at all — `intel_gpu_top` gets it from the perf
          PMU, which needs CAP_PERFMON and a privileged pod. `power/rc6_residency_ms` is the
          unprivileged equivalent: a monotonic counter of milliseconds the GPU spent in its
          RC6 idle state. Busy fraction is its complement, so a panel reads

              1 - rate(homelab_gpu_rc6_residency_seconds_total[5m])

          which is why that series is a COUNTER and the amdgpu one is a GAUGE.

CARDS ARE DISCOVERED, NEVER INDEXED. The card number is not stable: daniel-box's is `card1`
and daniel-server's is `card0`, and box's node was renumbered by the 2026-09-09 20:10 reboot.
Every path here is walked from `/sys/class/drm/card*` and branched on the driver the card's
device symlink resolves to.

Reads only world-readable sysfs files (`-r--r--r--`, checked on both nodes), so the pod is
unprivileged and runs as nobody with `/host/sys` mounted read-only.
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_SYSFS = "/host/sys"

# `homelab_` rather than `node_` or `drm_`: node_exporter's own (disabled) drm collector emits
# `node_drm_*` from the same amdgpu files, and a name collision between two exporters scraped
# into one Prometheus is a silently merged series.
_INFO = "homelab_gpu_info"
_BUSY = "homelab_gpu_busy_percent"
_RC6 = "homelab_gpu_rc6_residency_seconds_total"
_FREQ = "homelab_gpu_frequency_mhz"

_HELP = {
    _INFO: (
        "gauge",
        "Always 1, one series per discovered DRM card, labelled by driver.",
    ),
    _BUSY: ("gauge", "GPU busy percentage (amdgpu only; i915 exposes no equivalent)."),
    _RC6: (
        "counter",
        "Seconds spent in the RC6 idle state (i915 only); busy fraction is 1 - rate() of this.",
    ),
    _FREQ: ("gauge", "Current GPU clock in MHz."),
}


def _read_int(path: Path) -> int | None:
    """First integer in a sysfs file, or None when it is absent or unreadable.

    Returns None rather than raising: a driver that drops a file across a kernel bump must cost
    one series, not the whole scrape.
    """
    try:
        return int(path.read_text().strip().split()[0])
    except OSError, ValueError, IndexError:
        return None


def discover_cards(sysfs_root: str | Path = DEFAULT_SYSFS) -> list[tuple[str, str]]:
    """Every DRM card under `<sysfs_root>/class/drm`, as (card name, driver name).

    Sorted by card name so the exposition order is stable between scrapes.

    THE DRIVER LINK MUST BE PROVED TO EXIST BEFORE IT IS RESOLVED. `card[0-9]*` also matches
    every connector — `card1-DP-1`, `card1-HDMI-A-1`, `card1-Writeback-1` — whose own `device`
    symlink points back at the card but which carry no `device/driver` of their own. On a
    missing path `Path.resolve()` raises nothing and returns the path unchanged, so resolving
    first named all nine of daniel-box's connectors as a card with the driver `"driver"`.
    Requiring `exists()` (which follows the symlink) is what distinguishes them.
    """
    root = Path(sysfs_root) / "class" / "drm"
    cards = []
    for card in sorted(root.glob("card[0-9]*")):
        link = card / "device" / "driver"
        try:
            if not link.exists():
                continue
            driver = link.resolve().name
        except OSError:
            continue
        if driver:
            cards.append((card.name, driver))
    return cards


def card_samples(
    sysfs_root: str | Path, card: str, driver: str
) -> list[tuple[str, dict[str, str], float]]:
    """The (metric, labels, value) samples one card contributes.

    Every file read here is optional. A driver that exposes none of them still yields the
    `homelab_gpu_info` series, which is what makes "this node has a GPU and it reports nothing"
    distinguishable from "the exporter is not reading sysfs at all".
    """
    base = Path(sysfs_root) / "class" / "drm" / card
    labels = {"card": card, "driver": driver}
    out: list[tuple[str, dict[str, str], float]] = [(_INFO, labels, 1.0)]

    busy = _read_int(base / "device" / "gpu_busy_percent")
    if busy is not None:
        out.append((_BUSY, labels, float(busy)))

    rc6_ms = _read_int(base / "power" / "rc6_residency_ms")
    if rc6_ms is not None:
        out.append((_RC6, labels, rc6_ms / 1000.0))

    # i915 reports MHz directly at the card; amdgpu reports Hz through hwmon. Normalised to MHz
    # so one panel plots both, rather than a per-driver unit the reader has to remember.
    freq_mhz = _read_int(base / "gt_act_freq_mhz")
    if freq_mhz is None:
        for hwmon in sorted((base / "device" / "hwmon").glob("hwmon*")):
            hz = _read_int(hwmon / "freq1_input")
            if hz is not None:
                freq_mhz = hz // 1_000_000
                break
    if freq_mhz is not None:
        out.append((_FREQ, labels, float(freq_mhz)))

    return out


def collect(
    sysfs_root: str | Path = DEFAULT_SYSFS,
) -> list[tuple[str, dict[str, str], float]]:
    """Every sample from every discovered card."""
    samples: list[tuple[str, dict[str, str], float]] = []
    for card, driver in discover_cards(sysfs_root):
        samples.extend(card_samples(sysfs_root, card, driver))
    return samples


def render(samples: list[tuple[str, dict[str, str], float]]) -> str:
    """Prometheus text exposition.

    HELP/TYPE are emitted once per family and only for families that have a sample, because a
    TYPE line with no sample under it is a parse error in some clients and noise in the rest.
    """
    lines: list[str] = []
    for name, (kind, help_text) in _HELP.items():
        family = [s for s in samples if s[0] == name]
        if not family:
            continue
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, kind))
        for _, labels, value in family:
            rendered = ",".join('%s="%s"' % (k, v) for k, v in sorted(labels.items()))
            lines.append("%s{%s} %g" % (name, rendered, value))
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    sysfs_root = DEFAULT_SYSFS

    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render(collect(self.sysfs_root)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Log nothing.

        One line per scrape at 1m across two pods is 2880 Loki lines a day for nothing — the
        shape that made node-exporter's probes 97% of this namespace's ingest.

        The signature restates `BaseHTTPRequestHandler.log_message`'s exactly, unused arguments
        and shadowed builtin included: narrowing it to `*args` is an incompatible override.
        """


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--sysfs-root", default=DEFAULT_SYSFS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="print one exposition to stdout and exit, instead of serving",
    )
    args = parser.parse_args(argv)

    if args.once:
        sys.stdout.write(render(collect(args.sysfs_root)))
        return 0

    _Handler.sysfs_root = args.sysfs_root
    HTTPServer(("", args.port), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
