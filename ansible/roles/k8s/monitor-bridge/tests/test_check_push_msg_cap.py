"""The push boundary caps `msg` so Discord cannot reject the alert (#2013).

Kuma puts a push monitor's `msg` into the Discord DOWN embed as a field value capped at 1024
chars and never truncates, so an oversized msg is a 400 and a page nobody receives. Each rule
is an accept/reject pair, and the last test drives the real `push` to prove the cap sits on the
wire path rather than beside it.
"""

import urllib.parse

import bridge.net
from bridge.net import PUSH_MSG_MAX, cap_push_msg


def test_a_msg_under_the_cap_passes_verbatim():
    msg = "x" * PUSH_MSG_MAX
    assert cap_push_msg(msg) == msg


def test_a_msg_over_the_cap_is_cut_to_the_cap_with_a_count_marker():
    capped = cap_push_msg("a" * 2000)
    assert len(capped) == PUSH_MSG_MAX
    assert capped.endswith(" …(+%d chars)" % (2000 - capped.index(" …")))
    assert capped.startswith("a" * 100)


def test_the_cycles_suffix_survives_the_cut():
    # `probe_lib/alerts.py` strips ` (N cycles)` end-anchored, so the marker goes before it.
    capped = cap_push_msg("b" * 2000 + " (7 cycles)")
    assert len(capped) == PUSH_MSG_MAX
    assert capped.endswith(" chars) (7 cycles)")


def test_push_sends_the_capped_msg(cfg):
    urls = []
    bridge.net.push(
        cfg, "tok", False, "c" * 3000, fetch=lambda url: urls.append(url) or {}
    )
    assert len(urls) == 1
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)["msg"][0]
    assert len(sent) == PUSH_MSG_MAX
    assert sent.endswith(" chars)")
