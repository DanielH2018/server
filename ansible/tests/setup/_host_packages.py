"""The host packages, rendered once per run, for tests that read what a host ships.

Typical usage example:

    text = shipped_text("daniel-box", "/etc/systemd/journald.conf.d/50-homelab.conf")
"""

import functools

from renderer.renderer_lib import host as host_mode

HOSTS = ("daniel-box", "daniel-server", "daniel-pi")


@functools.cache
def package(host: str) -> host_mode.Package:
    """The rendered package of `host`."""
    return host_mode.render_host(host)


def shipped_text(host: str, path: str) -> str:
    """The text of `path` as `host`'s package installs it."""
    return package(host).files[path][0].decode()
