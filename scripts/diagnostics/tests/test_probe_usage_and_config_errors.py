"""Only a failed read exits TEMP_FAIL (75); a bad argument or a repo config error does not (#4471).

75 tells a caller such as land.sh to retry. A bad `--since` can never succeed on retry, so it
exits a usage code, and a key missing from group_vars stays a failure. The raises that really are
failed reads -- the HA websocket refusing the token, a server answering with a non-JSON body --
carry 75 themselves rather than relying on a blanket catch in `probe.main`. Each test drives the
real code and stubs only the transport under it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_usage_and_config_errors.py
"""

import builtins
import io
import json
import os
import socket
import ssl
import subprocess
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics.probe_lib import core, ha
from lib.exit_codes import FAILED, TEMP_FAIL, USAGE_ERROR


def _exit_code(call):
    """The code the process would exit with: a returned int, or a raised SystemExit's code.

    A SystemExit carrying a message exits 1, which is what the interpreter does with it.
    """
    try:
        return call()
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else FAILED


def _last_line(out):
    lines = out.rstrip("\n").splitlines()
    return lines[-1] if lines else ""


def test_bad_since_last_line_is_not_unreadable_verdict(capsys):
    _exit_code(lambda: probe.main(["b2-deletions", "--since", "3x"]))
    last = _last_line(capsys.readouterr().out)
    assert not last.startswith("PROBE-VERDICT: unreadable"), last


def test_bad_since_exits_usage_error(capsys):
    rc = _exit_code(lambda: probe.main(["b2-deletions", "--since", "3x"]))
    assert rc == USAGE_ERROR


def test_group_vars_key_missing_stays_a_failure(monkeypatch, capsys):
    # SOPS answers the domain through `subprocess.run`. The group_vars read is the repo config
    # under test, served here without `k3s_metallb_ingress_vip`; every other open is real.
    real_open = builtins.open

    def open_without_vip(path, *args, **kwargs):
        if path == core.GROUP_VARS_PATH:
            return io.StringIO("k8s_namespace: homelab\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_vip)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 0, "example.test\n", ""),
    )
    rc = _exit_code(lambda: probe.main(["b2-deletions", "--since", "1h"]))
    assert rc == FAILED, f"a missing group_vars key exited {rc}"


def _server_frame(msg):
    payload = json.dumps(msg).encode()
    return bytes([0x81, len(payload)]) + payload


class _FakeSock:
    """A websocket peer that upgrades, asks for auth, then refuses the token."""

    def __init__(self):
        self._data = (
            b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
            + _server_frame({"type": "auth_required"})
            + _server_frame({"type": "auth_invalid", "message": "Invalid access token"})
        )

    def sendall(self, data):
        pass

    def recv(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def close(self):
        pass


class _FakeContext:
    minimum_version = None

    def wrap_socket(self, raw, server_hostname=None):
        return raw


def test_ha_websocket_auth_refusal_exits_unreadable(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: _FakeContext())
    with pytest.raises(SystemExit) as exc:
        ha.ha_trace("ha.local.example.test", "bad-token", "some_automation")
    assert exc.value.code == TEMP_FAIL


def test_non_json_response_exits_unreadable():
    with pytest.raises(SystemExit) as exc:
        core.parse_json("https://loki.example.test/q", "<html>502 Bad Gateway</html>")
    assert exc.value.code == TEMP_FAIL
