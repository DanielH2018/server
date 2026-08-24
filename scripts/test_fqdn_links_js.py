"""Tests for docs/assets/fqdn-links.js — the browser half of the route links.

WHY THIS IS TESTED AT ALL. The docs site answers on two names, `docs.<domain>` and
`docs.local.<domain>`, and the script recovers the domain from whichever one the reader
arrived on. Getting that wrong in one direction leaves every route link on the site
pointing at a name that does not resolve, and nothing else in the build would notice --
`mkdocs build --strict` checks internal links, not ones assembled at runtime.

HOW. The real script runs under node against a stubbed DOM holding one span, and the
test reads back the href it produced. Driving it end to end rather than reaching for the
private derivation is what makes this a test of the file the site actually loads.

Skipped where node is absent, which is why these assertions live here rather than in a
browser harness the repo does not have.

Run: uv run pytest scripts/test_fqdn_links_js.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "docs" / "assets" / "fqdn-links.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

# A DOM with exactly enough surface for the script: one span carrying data-host, and a
# replaceWith that records what the script swapped in.
_HARNESS = """
const hostname = process.argv[1];
const dataHost = process.argv[2];
let replacement = null;
const span = {
  getAttribute: (k) => (k === "data-host" ? dataHost : null),
  replaceWith: (node) => { replacement = node; },
};
globalThis.document = {
  readyState: "complete",
  addEventListener: () => {},
  querySelectorAll: () => [span],
  createElement: () => ({ href: null, textContent: null, target: null, rel: null }),
};
globalThis.window = { location: { hostname: hostname } };
require(SCRIPT_PATH);
console.log(JSON.stringify(replacement));
"""


def _linkify(hostname: str, data_host: str = "sonarr.local") -> dict | None:
    """The element the script produced for one span, or None if it left it alone."""
    harness = _HARNESS.replace("SCRIPT_PATH", json.dumps(str(SCRIPT)))
    result = subprocess.run(
        ["node", "-e", harness, "--", hostname, data_host],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed: {result.stderr.strip()[:800]}")
    return json.loads(result.stdout.strip())


def test_the_public_docs_name_produces_a_public_link():
    link = _linkify("docs.example.com", "sonarr")
    assert link["href"] == "https://sonarr.example.com"
    assert link["textContent"] == "sonarr.example.com"


def test_the_lan_docs_name_produces_a_lan_link():
    """Both names must resolve to one domain, or one tier's links all break."""
    link = _linkify("docs.local.example.com", "sonarr.local")
    assert link["href"] == "https://sonarr.local.example.com"


def test_a_deeper_domain_keeps_every_remaining_label():
    link = _linkify("docs.local.a.b.example.com", "sonarr")
    assert link["href"] == "https://sonarr.a.b.example.com"


def test_an_ip_literal_leaves_the_placeholder_alone():
    """Dropping the first octet of 127.0.0.1 gives "0.0.1", which looks like a domain and
    is not one. mkdocs serve and a direct ClusterIP dial both land here."""
    assert _linkify("127.0.0.1") is None


def test_a_bare_hostname_leaves_the_placeholder_alone():
    assert _linkify("localhost") is None


def test_a_two_label_hostname_leaves_the_placeholder_alone():
    """Stripping the first label would leave "com" — not a host anyone can reach."""
    assert _linkify("docs.example") is None


def test_a_span_with_no_data_host_is_skipped():
    assert _linkify("docs.example.com", "") is None


def test_links_open_in_a_new_tab_without_leaking_the_opener():
    link = _linkify("docs.example.com", "sonarr")
    assert link["target"] == "_blank"
    assert link["rel"] == "noopener"
