"""Helpers for the activation and ufw-arm suites: shipped files, a ufw host, and red fixtures.

A red fixture is a copy of reconcile with one rule removed. The suite runs the scenario against
the copy and expects its assertions to fail, which proves the scenario can see the rule.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from _reconcile_host import RECONCILE, Node

LIB = RECONCILE.parent
TEXTFILE = "var/lib/node-exporter-textfile"
PROM = f"{TEXTFILE}/homelab_host_reconcile.prom"


def base(node: Node) -> Node:
    """The units every host package carries, plus the textfile directory the exporter owns."""
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-updater.timer")
    node.unit("homelab-reconcile.service", policy="manual")
    (node.root / TEXTFILE).mkdir(parents=True)
    return node


def ship(node: Node, path: str, text: str, action: str) -> None:
    """Install `text` at `path` under the root and declare `action` for it in the manifest."""
    target = node.root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    node.manifest.setdefault("activations", {})[path] = action


def live_unit(node: Node, name: str, **kwargs: str) -> None:
    node.state["units"][name] = {
        "load": "loaded",
        "active": kwargs.get("active", "active"),
        "enabled": kwargs.get("enabled", "enabled"),
    }


def metrics(node: Node) -> str:
    return (node.root / PROM).read_text()


def verbs(node: Node) -> list[list[str]]:
    """Each command that could change the host, as argv: the systemctl actions and the rest."""
    changing = [call["argv"] for call in node.calls() if call["argv"][0] != "hostname"]
    return [
        argv
        for argv in changing
        if argv[0] in ("sysctl", "sshd", "fail2ban-client", "ufw")
        or argv[:2] in (["systemctl", "reload"], ["systemctl", "try-restart"])
        or argv[:2] == ["systemctl", "restart"]
    ]


def mutated(tmp_path: Path, where: str, old: str, new: str) -> Node:
    """A Node whose reconcile is a copy of the real one with `old` replaced by `new` in `where`."""
    copy = tmp_path / "mutant"
    shutil.copytree(LIB / "reconcile_lib", copy / "reconcile_lib")
    shutil.copy2(RECONCILE, copy / RECONCILE.name)
    target = copy / where
    text = target.read_text()
    assert old in text, f"{where} no longer contains {old!r}: update the red fixture"
    target.write_text(text.replace(old, new, 1))
    (tmp_path / "node").mkdir()
    return base(Node(tmp_path / "node", script=copy / RECONCILE.name))


def red(
    tmp_path: Path, scenario: Callable[[Node], None], where: str, old: str, new: str
) -> None:
    """Assert that `scenario` fails against reconcile with `old` replaced by `new`."""
    with pytest.raises(AssertionError):
        scenario(mutated(tmp_path, where, old, new))
