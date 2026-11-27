import json

from lib.check_verdict import export_verdict

# 2026-10-18T05:30:26Z
_NOW = 1792301426.0


def _recorder(reply: tuple[int, str]):
    calls = []

    def kubectl(*argv):
        calls.append(argv)
        return reply

    return kubectl, calls


def test_an_accepted_patch_writes_the_verdict_annotations_under_the_field_manager():
    kubectl, calls = _recorder((0, ""))
    assert (
        export_verdict(kubectl, "x-health-verdict", "x-health", "down", "m" * 300, _NOW)
        is None
    )
    [argv] = calls
    assert argv[:4] == ("patch", "configmap", "x-health-verdict", "--type=merge")
    assert [a for a in argv if a.startswith("--field-manager=")] == [
        "--field-manager=x-health"
    ]
    [patch] = [a for a in argv if a.startswith("--patch=")]
    assert json.loads(patch.removeprefix("--patch=")) == {
        "metadata": {
            "annotations": {
                "homelab/check-verdict": "down",
                "homelab/check-last-run": "2026-10-18T05:30:26Z",
                "homelab/check-message": "m" * 200,
            }
        }
    }


def test_a_refused_patch_returns_the_line_naming_the_configmap_and_the_reason():
    kubectl, _ = _recorder((1, "Error from server (Forbidden)\n"))
    assert (
        export_verdict(kubectl, "x-health-verdict", "x-health", "up", "ok", _NOW)
        == "export failed: cannot patch x-health-verdict: Error from server (Forbidden)"
    )
