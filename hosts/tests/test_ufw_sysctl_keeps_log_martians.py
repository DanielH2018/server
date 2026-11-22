"""Guards that UFW's own sysctl file must not undo the host's martian logging.

Two files set `log_martians`: host-tuning's sysctl.d file, and UFW's `/etc/ufw/sysctl.conf`
(`IPT_SYSCTL` in /etc/default/ufw), which UFW re-applies on every `ufw enable`/`reload` and at
boot. Ubuntu
ships the second with `net/ipv4/conf/{all,default}/log_martians=0` uncommented, so unless the
host-tuning component rewrites that file too, the next `ufw reload` puts the key back to 0.

The source is the rendered host package: `host-tuning/component.yml`'s `lines:` section, which
the reconcile line arm applies before the ufw arm (`hosts/lib/homelab-reconcile`, `ARMS` and
`EARLY`), and its sysctl.d file. What matters is that both files carry the same value and the
sysctl.d file is activated.

The census names the edits it must find, so a rename fails with the member that went missing
rather than passing over an empty list.

Run: uv run pytest hosts/tests/test_ufw_sysctl_keeps_log_martians.py
"""

import pytest
from _host_packages import HOSTS, package

UFW_SYSCTL = "/etc/ufw/sysctl.conf"
SYSCTL_D = "/etc/sysctl.d/90-homelab-host-tuning.conf"
INTERFACES = ("all", "default")


def _edits(host: str, path: str) -> list[dict]:
    """The line edits `host`'s rendered package makes to `path`."""
    return [e for e in package(host).lines if e["path"] == path]


def _rewrites_ufw_log_martians(edit: dict) -> bool:
    """True when this edit rewrites one of UFW's log_martians keys to 1."""
    return (
        edit.get("path") == UFW_SYSCTL
        and "log_martians" in str(edit.get("regexp", ""))
        and str(edit.get("line", "")).endswith("log_martians=1")
    )


@pytest.mark.parametrize("host", HOSTS)
def test_the_component_rewrites_ufws_copy_of_the_log_martians_keys(host: str):
    hits = sorted(
        e["line"] for e in _edits(host, UFW_SYSCTL) if _rewrites_ufw_log_martians(e)
    )
    assert hits == [f"net/ipv4/conf/{iface}/log_martians=1" for iface in INTERFACES], (
        hits
    )


def test_an_edit_that_leaves_ufws_copy_alone_is_flagged():
    """The rejecting half: the same predicate over the shapes it must NOT accept."""
    assert not _rewrites_ufw_log_martians(
        {"path": SYSCTL_D, "regexp": "log_martians", "line": "x=1"}
    )
    assert not _rewrites_ufw_log_martians(
        {
            "path": UFW_SYSCTL,
            "regexp": "^net/ipv4/ip_forward=",
            "line": "net/ipv4/ip_forward=1",
        }
    )
    assert not _rewrites_ufw_log_martians(
        {
            "path": UFW_SYSCTL,
            "regexp": "log_martians",
            "line": "net/ipv4/conf/all/log_martians=0",
        }
    )


@pytest.mark.parametrize("host", HOSTS)
def test_the_sysctl_d_file_and_ufws_copy_agree_and_the_live_value_is_activated(
    host: str,
):
    """UFW's reload re-asserts the same 1 the live value was set to."""
    shipped = package(host).files[SYSCTL_D][0].decode().splitlines()
    assert sorted(line for line in shipped if "log_martians" in line) == [
        f"net.ipv4.conf.{iface}.log_martians=1" for iface in INTERFACES
    ]
    assert package(host).activations[SYSCTL_D] == "sysctl", (
        f"{SYSCTL_D} is not activated, so the file changes and the live value does not"
    )
