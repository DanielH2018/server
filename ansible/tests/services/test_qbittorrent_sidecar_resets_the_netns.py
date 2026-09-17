"""qbittorrent's wireguard sidecar resets the pod netns before its own init, on every start.

The pod's network namespace outlives the sidecar container, so a container restart inherits the
dead wg0, wg-quick's policy rules, the LAN routes and the kill-switch REJECT from the previous
one, and the Mullvad mod cannot bring a tunnel up behind them. That deadlock cost 9h/107
restarts on 2026-08-16 and 3.5d/1005 restarts on 2026-09-13 (#1838), cleared both times by a
pod delete. `files/netns-reset.sh` is what makes a container restart equivalent to that delete;
these guards hold the wiring that gets it run, and the two properties of the script itself that
a functional test cannot see without a netns: it gates before it tears down, and it fails closed.
"""

import re

from _helpers import ANSIBLE
from _k8s_render import rendered_docs

SCRIPT = ANSIBLE / "roles/k8s/qbittorrent/files/netns-reset.sh"


def _sidecar():
    for role, _tpl, doc in rendered_docs():
        if role == "qbittorrent" and doc["kind"] == "Deployment":
            spec = doc["spec"]["template"]["spec"]
            return spec, next(
                c for c in spec["initContainers"] if c["name"] == "wireguard"
            )
    raise AssertionError("qbittorrent Deployment did not render")


def test_the_sidecar_runs_the_reset_before_init_and_keeps_init_as_pid_1():
    _spec, wg = _sidecar()
    command = " ".join(wg["command"])
    assert "netns-reset.sh && exec /init" in command, (
        f"the reset must run first and hand PID 1 to /init with exec, got {command!r}"
    )


def test_the_reset_script_is_mounted_from_the_configmap_that_carries_it():
    spec, wg = _sidecar()
    mount = next(m for m in wg["volumeMounts"] if m["name"] == "netns-reset")
    assert wg["command"][-1].startswith(mount["mountPath"] + "/"), (
        "the command path must sit under the ConfigMap mount"
    )
    volume = next(v for v in spec["volumes"] if v["name"] == "netns-reset")
    assert volume["configMap"]["defaultMode"] == 0o555, "the script must be executable"
    configmaps = {
        doc["metadata"]["name"]: doc
        for role, _t, doc in rendered_docs()
        if role == "qbittorrent" and doc["kind"] == "ConfigMap"
    }
    carried = configmaps[volume["configMap"]["name"]]["data"]["netns-reset.sh"]
    assert carried.strip() == SCRIPT.read_text().strip(), (
        "the ConfigMap must carry files/netns-reset.sh verbatim"
    )


def test_the_gate_scopes_to_the_uid_qbittorrent_actually_runs_as():
    # The script reads PUID from the SIDECAR's environment and qbittorrent runs as the PUID of
    # ITS container. If the two ever decouple the gate matches nothing and reads green — the
    # Pi run showed uid 911 egressing freely past a uid-1000 gate.
    spec, wg = _sidecar()
    qbt = next(c for c in spec["containers"] if c["name"] == "qbittorrent")
    env = lambda c: {e["name"]: e.get("value") for e in c["env"]}
    assert env(wg)["PUID"] == env(qbt)["PUID"], (
        "the sidecar's PUID must be the uid qbittorrent runs as"
    )


def test_the_gate_goes_up_before_the_stale_tunnel_comes_down():
    # The order is the whole privacy argument: qbittorrent keeps running while the sidecar
    # restarts, and between the reset and the mod's PostUp the netns has no tunnel. The
    # uid-scoped REJECT must be in place before wg0 is deleted, or that window leaks.
    text = SCRIPT.read_text()
    gate = text.index("--uid-owner ${uid}")
    teardown = text.index("ip link del wg0")
    assert gate < teardown, "install the uid gate before deleting wg0"
    assert re.search(r"^\s*exit 1\s*$", text[gate:teardown], re.M), (
        "a failed gate install must exit non-zero before anything is torn down"
    )
    assert "-m owner --uid-owner ${uid}" in text and "! -o wg0" in text, (
        "the gate scopes to qbittorrent's uid and exempts traffic leaving via wg0"
    )


def test_the_gate_exempts_wireguards_own_carrier_packets():
    # `-o wg0` is not enough. qbittorrent's packet enters wg0, and the encrypted carrier
    # WireGuard then emits on eth0 still belongs to the originating socket, so `--uid-owner`
    # matches it and `! -o wg0` is true: the gate rejected every carrier packet for uid 1000
    # on 2026-09-17 (DHT 0 nodes, every peer and tracker timing out, root's curl through the
    # same tunnel fine). wg-quick marks the carrier with fwmark 51820 (`wg set wg0 fwmark`),
    # which is exactly how the mod's own kill-switch exempts it; the gate must too.
    gate_line = next(
        line for line in SCRIPT.read_text().splitlines() if "--uid-owner ${uid}" in line
    )
    assert "-m mark ! --mark 51820" in gate_line, (
        "the uid gate must exempt WireGuard's fwmark-51820 carrier packets, or it rejects "
        "the tunnel it is meant to force traffic through"
    )
