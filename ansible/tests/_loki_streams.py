"""The labels loki-homelab's streams carry, and how a LogQL stream selector matches them.

Two guards hold selectors to the streams Alloy emits:
`ansible/roles/k8s/monitor-bridge/tests/test_check_loki.py` for monitor-bridge's selectors and
`ansible/tests/services/test_loki_stream_labels.py` for the Loki ruler's rule files. Both read
the label vocabulary, the Pi's streams and the matcher from here, so the two cannot disagree
about what a selector selects.
"""

import re

from _helpers import ALL_VARS, load_yaml
from lib.repo_paths import REPO

LOKI_STREAMS = load_yaml(ALL_VARS)["loki_streams"]

# The k8s pod stream vocabulary, read off a live Loki stream. `app` is NOT in it — a
# selector with app="home-assistant" matches no stream and reports "no ip_ban events" forever. A fail-open arm cannot tell "nothing to report" from "wrong question",
# so the selector label has to be checked by something other than the check's own verdict.
# Transcribed from a live stream. Kept as a FLOOR rather than the whole answer:
# `filename`, `stream` and `service_name` are added by Alloy/Loki itself and appear in no
# config, so deriving alone would under-count and reject a valid selector.
_LOKI_STREAM_LABELS_OBSERVED = frozenset(
    {
        "container",
        "filename",
        "job",
        "machine",
        "namespace",
        "pod",
        "service_name",
        "stream",
    }
)


def _alloy_relabel_targets():
    """`target_label = "…"` values from the Alloy config — the labels it actually sets.

    Derived rather than transcribed because the transcription cannot follow a rename: renaming a
    relabel target leaves the frozenset above listing a label nothing emits, so a selector using
    the NEW name is rejected while one using the dead name passes — the guard reporting the
    opposite of the truth. Internal `__foo__` labels are dropped; they never reach a stream.
    """
    cfg = (
        REPO / "ansible/roles/k8s/loki-homelab/templates/config/config.alloy.j2"
    ).read_text()
    found = set(re.findall(r'target_label\s*=\s*"([^"]+)"', cfg))
    return {label for label in found if not label.startswith("__")}


LOKI_STREAM_LABELS = _LOKI_STREAM_LABELS_OBSERVED | _alloy_relabel_targets()

# daniel-pi's streams, by `loki_streams` role. The container stream carries a `container`
# label on top of the owner's, so it gets one of the Pi's real container names.
_PI_STREAMS = {
    role: {**labels, "container": "wg-easy"} if role == "pi_containers" else labels
    for role, labels in LOKI_STREAMS.items()
    if labels.get("machine") == "daniel-pi"
}


def _selector_labels(selector):
    """Label names in a LogQL stream selector — the `foo` of `{foo="bar",baz=~"qux"}`."""
    head = selector.split("}", 1)[0]
    return set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", head))


def _selector_matches(selector, labels):
    """Whether a LogQL stream selector matches a stream carrying `labels`.

    Loki semantics: a matcher on an absent label compares against the empty string, so
    `machine!="daniel-pi"` matches a stream with no `machine` label, and a regex is anchored.
    """
    head = selector.split("}", 1)[0]
    for name, op, value in re.findall(r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"', head):
        got = labels.get(name, "")
        if op == "=":
            ok = got == value
        elif op == "!=":
            ok = got != value
        elif op == "=~":
            ok = re.fullmatch(value, got) is not None
        else:
            ok = re.fullmatch(value, got) is None
        if not ok:
            return False
    return True
