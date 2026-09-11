"""`scripts/validate/renovate_config.sh` retries the Renovate install, never the validation.

The install fails for a few minutes after every Renovate npm publish (a tarball 404 on a
version `latest` already names), so it loops with a backoff. The validation is the verdict,
so it runs once. `npm` and the installed `renovate-config-validator` are faked on PATH; each
records its calls to a log the assertions read.

Run: uv run pytest scripts/validate/tests/test_renovate_config_retry.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "validate" / "renovate_config.sh"

# Fails until the call count reaches NPM_FAILURES, then "installs" a validator stub whose
# exit code is VALIDATOR_EXIT. Records every call so the tests can count attempts.
FAKE_NPM = """#!/usr/bin/env bash
set -euo pipefail
echo "npm $*" >> "$CALL_LOG"
count=$(grep -c '^npm ' "$CALL_LOG")
if [ "$count" -le "${NPM_FAILURES:-0}" ]; then
  echo "npm error 404 Not Found - GET https://registry.npmjs.org/renovate/-/renovate-0.0.0.tgz" >&2
  exit 1
fi
prefix=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--prefix" ]; then prefix="$2"; shift; fi
  shift
done
mkdir -p "$prefix/node_modules/.bin"
cat > "$prefix/node_modules/.bin/renovate-config-validator" <<EOF
#!/usr/bin/env bash
echo "validator \\$*" >> "$CALL_LOG"
exit ${VALIDATOR_EXIT:-0}
EOF
chmod +x "$prefix/node_modules/.bin/renovate-config-validator"
"""


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "bin").mkdir()
    npm = tmp_path / "bin" / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    shutil.copy2(SCRIPT, tmp_path / "renovate_config.sh")
    (tmp_path / "renovate.json").write_text("{}\n")
    return tmp_path


def run(
    sandbox: Path, *, npm_failures: int = 0, validator_exit: int = 0, attempts: int = 6
):
    env = dict(os.environ)
    env["PATH"] = f"{sandbox / 'bin'}{os.pathsep}{env['PATH']}"
    env["CALL_LOG"] = str(sandbox / "calls.log")
    env["NPM_FAILURES"] = str(npm_failures)
    env["VALIDATOR_EXIT"] = str(validator_exit)
    env["RENOVATE_INSTALL_ATTEMPTS"] = str(attempts)
    env["RENOVATE_INSTALL_BACKOFF"] = "0"
    env["RUNNER_TEMP"] = str(sandbox / "tmp")
    return subprocess.run(
        [str(sandbox / "renovate_config.sh")],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
    )


def calls(sandbox: Path) -> list[str]:
    log = sandbox / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_a_tarball_404_is_retried_and_the_validator_runs_once(sandbox):
    result = run(sandbox, npm_failures=2)
    assert result.returncode == 0, result.stderr
    recorded = calls(sandbox)
    assert sum(c.startswith("npm ") for c in recorded) == 3
    assert [c for c in recorded if c.startswith("validator ")] == [
        "validator --strict renovate.json"
    ]


def test_an_install_that_never_succeeds_stops_at_the_attempt_cap(sandbox):
    result = run(sandbox, npm_failures=99, attempts=3)
    assert result.returncode == 1
    recorded = calls(sandbox)
    assert sum(c.startswith("npm ") for c in recorded) == 3
    assert not any(c.startswith("validator ") for c in recorded)
    assert "did not install after 3 attempts" in result.stderr


def test_a_validation_failure_is_final_and_never_retried(sandbox):
    result = run(sandbox, validator_exit=2)
    assert result.returncode == 2
    recorded = calls(sandbox)
    assert sum(c.startswith("npm ") for c in recorded) == 1
    assert sum(c.startswith("validator ") for c in recorded) == 1
