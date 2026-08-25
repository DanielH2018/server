"""Tests for the DNS witness used to measure Pi-hole rollout gaps.

The point of this module is that a sample counts as OK only for a genuine answer.
A witness that accepted any UDP datagram back would report a clean run through a
SERVFAIL storm, which is exactly the failure it exists to catch.
"""

import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dns_witness  # noqa: E402


def _serve_one(sock: socket.socket, flags: int, ancount: int, txid_override=None):
    """Answer exactly one query with a header built to order, then stop."""
    try:
        data, addr = sock.recvfrom(2048)
    except OSError:
        return
    txid = struct.unpack(">H", data[:2])[0] if txid_override is None else txid_override
    reply = struct.pack(">HHHHHH", txid, flags, 1, ancount, 0, 0) + data[12:]
    sock.sendto(reply, addr)


@pytest.fixture
def responder():
    """Start a one-shot UDP responder; yields a factory returning its port."""
    sockets = []
    threads = []

    def start(flags=0x8180, ancount=1, txid_override=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sockets.append(sock)
        thread = threading.Thread(
            target=_serve_one, args=(sock, flags, ancount, txid_override), daemon=True
        )
        thread.start()
        threads.append(thread)
        return sock.getsockname()[1]

    yield start
    for sock in sockets:
        sock.close()
    for thread in threads:
        thread.join(timeout=2)


def test_answer_with_rcode_zero_is_ok(responder):
    port = responder()
    assert dns_witness.query("127.0.0.1", "pi.hole", port) is True


def test_servfail_is_not_ok(responder):
    port = responder(flags=0x8182)
    assert dns_witness.query("127.0.0.1", "pi.hole", port) is False


def test_zero_answers_is_not_ok(responder):
    """Pi-hole returning NOERROR with no records is not a working resolver."""
    port = responder(ancount=0)
    assert dns_witness.query("127.0.0.1", "pi.hole", port) is False


def test_mismatched_transaction_id_is_not_ok(responder):
    port = responder(txid_override=0x1111)
    assert dns_witness.query("127.0.0.1", "pi.hole", port) is False


def test_timeout_is_not_ok(monkeypatch):
    monkeypatch.setattr(dns_witness, "TIMEOUT", 0.2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert dns_witness.query("127.0.0.1", "pi.hole", port) is False


def test_query_encodes_the_name_as_dns_labels(responder):
    """The label encoding is hand-rolled, so pin it against a real parse."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.settimeout(2)
    threading.Thread(
        target=dns_witness.query, args=("127.0.0.1", "pi.hole", port), daemon=True
    ).start()
    data, _ = sock.recvfrom(2048)
    sock.close()
    assert data[12:] == b"\x02pi\x04hole\x00" + struct.pack(">HH", 1, 1)
