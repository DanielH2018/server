"""bridge.common's docstrings name the pager the bridges use since Cutover-2, not Uptime Kuma.

Neither bridge pushes to Kuma: monitor-bridge stopped at M6 A6's Cutover-2 and autofix-bridge at
B7's. Each exports its verdicts to Prometheus, and Alertmanager delivers a firing alert to
Discord (#7060). The `kuma-push-lib.sh` mention inside another docstring is out of scope here.
"""

from bridge import common


def _words(doc: str | None) -> str:
    assert doc, "docstring is missing"
    return " ".join(doc.split())


def test_module_docstring_names_no_kuma_push():
    assert "kuma" not in _words(common.__doc__).lower()


def test_sanitize_docstring_names_no_kuma_to_discord_delivery():
    assert "kuma" not in _words(common.sanitize.__doc__).lower()


def test_module_docstring_says_the_bridges_export_verdicts_to_prometheus():
    assert "Prometheus" in _words(common.__doc__)


def test_sanitize_docstring_says_alertmanager_delivers_to_discord():
    doc = _words(common.sanitize.__doc__)
    assert "Alertmanager" in doc
    assert "Discord" in doc
