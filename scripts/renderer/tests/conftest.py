"""The determinism digest printer, and the `hosts` fixture the host-mode tests share.

The determinism modules' digest lines print to the job log.

pytest loads this file in the controller as well as in every xdist worker, because CI names
the test files under this directory on its command line. `_render_audit.log_digest_lines` says
why the printing happens here and not in the test.
"""

import pytest
import yaml
from _render_audit import log_digest_lines


def pytest_terminal_summary(terminalreporter):
    log_digest_lines(terminalreporter)


@pytest.fixture
def hosts(tmp_path):
    """A writer for a `hosts/` tree: one package spec and its components."""
    root = tmp_path / "hosts"

    def write(
        package: dict,
        components: dict[str, dict] | None = None,
        sources: dict[str, str] | None = None,
        where: str = "daniel-pi",
    ):
        spec = root / where / "package.yml"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(yaml.safe_dump(package))
        for name, component in (components or {}).items():
            directory = root / "components" / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "component.yml").write_text(yaml.safe_dump(component))
            for src, text in (sources or {}).items():
                (directory / src).parent.mkdir(parents=True, exist_ok=True)
                (directory / src).write_text(text)
        return root

    return write
