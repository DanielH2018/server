"""The `closed_url` fixture of test_n8n_verify.py keeps its port refused for the whole test.

An isolation leg passes only on a refused connect. When the fixture releases its port, the
next local server, in that test or in another xdist worker, can be handed the same port, and
the leg then reads an answer (#6480). The fixture stays in test_n8n_verify.py; this module
pulls it by name.

Run: uv run pytest ansible/roles/k8s/n8n/tests/test_n8n_verify_closed_port.py
"""

import socket
from urllib.parse import urlsplit

from test_n8n_verify import closed_url

__all__ = ["closed_url"]


def test_no_other_socket_can_claim_the_closed_port(request):
    """A port another socket can bind is one a stand-in server can answer on."""
    url = request.getfixturevalue("closed_url")
    parts = urlsplit(url)
    with socket.socket() as other:
        try:
            other.bind((parts.hostname, parts.port))
        except OSError:
            return
    raise AssertionError(
        f"{url} is free to bind while the test holds it, so a local server can answer on it"
    )
