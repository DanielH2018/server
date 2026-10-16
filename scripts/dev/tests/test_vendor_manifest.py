"""`vendor_manifest.py`: the refresh against a fake fetcher, and its removed-object report.

The pin check itself is `ansible/tests/k8s/test_vendored_manifests_match_their_pins.py`.

Run: uv run pytest scripts/dev/tests/test_vendor_manifest.py
"""

import hashlib

from vendor_manifest import main, removed_objects

OLD = b"""\
apiVersion: policy/v1beta1
kind: PodDisruptionBudget
metadata: {name: speaker, namespace: demo-system}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: legacy, namespace: demo-system}
"""
NEW = b"""\
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: speaker, namespace: demo-system}
"""
DEFAULTS = """\
demo_version: v1.0.0
demo_vendored_manifests:
  # The comment the rewrite must keep.
  - name: demo.yaml  # a trailing comment the item match must allow
    url: "https://example.invalid/{{ demo_version }}/demo.yaml"
    fetched_url: "https://example.invalid/v1.0.0/demo.yaml"
    sha256: "%s"
"""


def _role(tmp_path, body=OLD):
    role = tmp_path / "k8s" / "demo"
    (role / "defaults").mkdir(parents=True)
    (role / "files" / "upstream").mkdir(parents=True)
    (role / "files" / "upstream" / "demo.yaml").write_bytes(body)
    (role / "defaults" / "main.yml").write_text(
        DEFAULTS % hashlib.sha256(body).hexdigest()
    )
    return role


def _serve(body, status=200):
    return lambda url: (status, iter([body]))


def test_a_bump_writes_the_file_and_both_pin_fields_and_keeps_comments(
    tmp_path, capsys
):
    role = _role(tmp_path)
    defaults = role / "defaults" / "main.yml"
    defaults.write_text(defaults.read_text().replace("v1.0.0", "v1.1.0", 1))

    assert main(["demo"], roles=tmp_path, fetcher=_serve(NEW)) == 0

    assert (role / "files" / "upstream" / "demo.yaml").read_bytes() == NEW
    text = defaults.read_text()
    assert 'fetched_url: "https://example.invalid/v1.1.0/demo.yaml"' in text
    assert f'sha256: "{hashlib.sha256(NEW).hexdigest()}"' in text
    assert "# The comment the rewrite must keep." in text


def test_a_bump_reports_the_removed_object_but_not_a_graduated_one():
    assert removed_objects(OLD, NEW) == ["ConfigMap demo-system/legacy"]


def test_changed_bytes_under_the_pinned_url_are_refused(tmp_path, capsys):
    role = _role(tmp_path)
    before = (role / "defaults" / "main.yml").read_text()

    assert main(["demo"], roles=tmp_path, fetcher=_serve(NEW)) == 1

    assert (role / "files" / "upstream" / "demo.yaml").read_bytes() == OLD
    assert (role / "defaults" / "main.yml").read_text() == before
    assert "Upstream changed the bytes under a pinned URL" in capsys.readouterr().err


def test_a_failed_fetch_writes_nothing(tmp_path, capsys):
    role = _role(tmp_path)

    assert main(["demo"], roles=tmp_path, fetcher=_serve(b"", status=404)) == 1

    assert (role / "files" / "upstream" / "demo.yaml").read_bytes() == OLD
    assert "HTTP 404" in capsys.readouterr().err


def test_an_unknown_file_name_is_a_usage_error(tmp_path, capsys):
    _role(tmp_path)

    assert main(["demo", "nope.yaml"], roles=tmp_path, fetcher=_serve(NEW)) == 64


def test_check_exits_1_on_a_drifted_file_and_0_on_a_pinned_one(tmp_path, capsys):
    role = _role(tmp_path)
    assert main(["--check"], roles=tmp_path) == 0

    (role / "files" / "upstream" / "demo.yaml").write_bytes(NEW)
    assert main(["--check"], roles=tmp_path) == 1
    assert "[FAIL] demo: demo.yaml hashes" in capsys.readouterr().err


def test_a_url_that_does_not_render_is_a_named_problem_not_a_traceback(
    tmp_path, capsys
):
    role = _role(tmp_path)
    defaults = role / "defaults" / "main.yml"
    defaults.write_text(
        defaults.read_text().replace("demo_version }}", "no_such_key }}")
    )

    assert main(["--check"], roles=tmp_path) == 1
    assert "demo.yaml: url does not render" in capsys.readouterr().err
