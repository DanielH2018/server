"""The host-package builder check for monitor-bridge.

Reads the builder's textfile series and every host's reconcile drift through Prometheus.
"""

import time

from bridge.config import Config
from bridge.sources import Sources
from verdicts.service import host_packages


def check_host_packages(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
    """Pages when the host-package builder stops fetching, or master stays unbuilt.

    Reads the builder's two textfile series off daniel-box's node exporter, the `node` job
    (`docs/host-packages.md`, *The builder's pages*). The row is gated on Prometheus and sits
    under `EXPORTER_DEPENDENT["node"]`: a dead exporter then pages once through Scrape Targets
    instead of also as an absent builder series. With Prometheus and the exporter both up, an
    absent series pages, because it means the builder's metric file never reached the exporter.
    The query names no origin, because only daniel-box runs the builder, and
    `bridge.net.origin_sel` would pin daniel-server.

    A third query reads `homelab_host_reconcile_drift` on every host and puts a non-zero count
    in the message. It never changes the verdict (`verdicts.service.host_packages`).
    """
    fetch = src.prom_scalar('homelab_builder_fetch_timestamp_seconds{job="node"}')
    unbuilt = src.prom_vector(
        'homelab_builder_unbuilt_since_timestamp_seconds{job="node"}'
    )
    # Every host's reconcile drift, so no `job=` selector: the Pi scrapes as `node-pi`.
    drift = src.prom_vector("homelab_host_reconcile_drift > 0")
    return host_packages(
        fetch,
        unbuilt,
        now if now is not None else time.time(),
        cfg.BUILDER_MAX_FETCH_AGE_S,
        cfg.BUILDER_MAX_LAG_S,
        drift,
    )
