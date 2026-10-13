"""`_k8s_render.prebuild` leaves a failed render to a test rather than raising it.

`ansible/tests/conftest.py` calls it when collection finishes, where an exception would end the
session in an INTERNALERROR naming no test.
"""

from _k8s_render import prebuild


def _broken_render():
    raise AssertionError("widget/deployment.yaml.j2 failed to render: boom")


def test_a_failed_render_does_not_escape_prebuild():
    prebuild(_broken_render)
