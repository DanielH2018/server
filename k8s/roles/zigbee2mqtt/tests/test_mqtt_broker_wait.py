"""zigbee2mqtt's `mqtt-broker-wait` init container waits for a CONNACK, not an open port (#8586).

Each test runs the init container's command from the rendered Deployment under `sh`. A stub
`nc` on `PATH` connects to a local socket that stands in for mosquitto, so the shell logic
around it (the CONNECT bytes, the CONNACK check, the retry loop) runs as the pod runs it.

Run: uv run pytest k8s/roles/zigbee2mqtt/tests/test_mqtt_broker_wait.py
"""

import socket
import sys
import threading
import time
from pathlib import Path

from _k8s_render import rendered_k8s_text
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

CONNACK_NOT_AUTHORISED = bytes([0x20, 0x02, 0x00, 0x05])

# Connects to the port in FAKE_BROKER_PORT, whatever host and port it was given, records its
# arguments, sends stdin, then copies the reply to stdout until the peer closes.
NC_STUB = f"""#!{sys.executable}
import os, socket, sys
with open(os.environ["NC_ARGS_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
try:
    s = socket.create_connection(("127.0.0.1", int(os.environ["FAKE_BROKER_PORT"])))
except OSError:
    sys.exit(1)
s.sendall(sys.stdin.buffer.read())
while chunk := s.recv(64):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
"""


def _wait_command() -> str:
    [deployment] = [
        d
        for d in yaml_fast.safe_load_all(
            rendered_k8s_text("zigbee2mqtt", "deployment.yaml.j2")
        )
        if d and d["kind"] == "Deployment"
    ]
    [container] = [
        c
        for c in deployment["spec"]["template"]["spec"]["initContainers"]
        if c["name"] == "mqtt-broker-wait"
    ]
    assert container["command"][:2] == ["sh", "-c"]
    return container["command"][2]


def _listener() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def _serve(sock: socket.socket, reply: bytes, received: list[bytes]) -> None:
    """Accept connections, record each CONNECT, and answer with `reply` (b"" = stay silent)."""

    held = []

    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            received.append(conn.recv(256))
            if reply:
                conn.sendall(reply)
                conn.close()
            else:
                held.append(conn)

    threading.Thread(target=loop, daemon=True).start()


def _run(command: str, tmp_path: Path, port: int, timeout: float = 30):
    bin_dir = fake_bin(tmp_path / "bin", nc=NC_STUB)
    env = {
        "PATH": "/usr/bin:/bin",
        "FAKE_BROKER_PORT": str(port),
        "NC_ARGS_LOG": str(tmp_path / "nc-args"),
    }
    return run(["sh", "-c", command], env=env, stub_bin=bin_dir, timeout=timeout)


def test_a_connack_refusing_the_anonymous_login_ends_the_wait(tmp_path):
    received: list[bytes] = []
    with _listener() as sock:
        _serve(sock, CONNACK_NOT_AUTHORISED, received)
        result = _run(_wait_command(), tmp_path, sock.getsockname()[1])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "nc-args").read_text().split("\n")[0] == "mosquitto 1883"
    [connect] = received
    assert connect[0] == 0x10 and connect[1] == len(connect) - 2
    assert connect[2:9] == b"\x00\x04MQTT\x04"


def test_a_broker_that_accepts_but_never_answers_keeps_z2m_waiting(tmp_path):
    command = _wait_command()
    assert command.startswith("timeout 180 ")
    received: list[bytes] = []
    with _listener() as sock:
        _serve(sock, b"", received)
        result = _run(
            command.replace("timeout 180 ", "timeout 5 ", 1),
            tmp_path,
            sock.getsockname()[1],
        )
    assert result.returncode == 124
    assert "waiting for a CONNACK from mosquitto:1883" in result.stdout
    assert received


def test_a_refused_port_is_retried_until_the_broker_answers(tmp_path):
    with _listener() as sock:
        port = sock.getsockname()[1]
    received: list[bytes] = []

    def start_late():
        time.sleep(3)
        late = socket.socket()
        late.bind(("127.0.0.1", port))
        late.listen()
        _serve(late, CONNACK_NOT_AUTHORISED, received)

    threading.Thread(target=start_late, daemon=True).start()
    result = _run(_wait_command(), tmp_path, port)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "waiting for a CONNACK" in result.stdout
    assert received
