"""Print the determinism modules' digest lines to the job log.

pytest loads this file in the controller as well as in every xdist worker, because CI names
the test files under this directory on its command line. `_render_audit.log_digest_lines` says
why the printing happens here and not in the test.
"""

from _render_audit import log_digest_lines


def pytest_terminal_summary(terminalreporter):
    log_digest_lines(terminalreporter)
