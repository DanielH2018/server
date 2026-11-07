"""R4's exit criterion 4: the renderer and the validator run with ansible-core unimportable.

Each check starts a subprocess that sets `sys.modules['ansible'] = None`, so `import ansible`
raises wherever the code reaches for it, a deferred import included. The determinism modules
render every tag the same way (`_render_audit_child.py`). Here are the validator, a host
package, a filter's refusal, and the red half: a shim that makes `renderer_lib.filters`, which
every render imports, import `ansible` first.

Run: uv run pytest scripts/renderer/tests/test_renderer_ansible_free.py
"""

import json
import sys
from pathlib import Path

from lib.proc_testing import run
from lib.repo_paths import REPO, SCRIPTS
from renderer import shadow_diff

BLOCK = "import sys\nsys.modules['ansible'] = None\n"
# Runs before the renderer's first import of `renderer_lib.filters`.
FILTERS_IMPORT_ANSIBLE = """
class FiltersImportAnsible:
    def find_spec(self, name, path=None, target=None):
        if name == "renderer.renderer_lib.filters":
            import ansible
        return None
sys.meta_path.insert(0, FiltersImportAnsible())
"""
RENDER_ONE_TAG = """
from pathlib import Path
from renderer import homelab_render
sys.exit(homelab_render.cluster(Path(sys.argv[1]), ["littlelink"], Path(sys.argv[2])))
"""
VALIDATE = """
from validate import k8s_manifests
sys.exit(k8s_manifests.main())
"""
RENDER_HOST = """
from renderer import homelab_render
sys.exit(homelab_render.main(["host", "--host", "daniel-pi", "--out", sys.argv[1]]))
"""
# An entry with no tier: `tier_priority_class` refuses it, and `render_tag` reports
# the refusal as the renderer's own error rather than a traceback.
REFUSE = """
import json
from renderer.renderer_lib.cluster import RenderError, render_tag
stubs = json.loads(sys.argv[1])
stubs["containers_list"] = [
    {"name": "authelia", "platform": "k8s"},
    {"name": "sonarr", "platform": "k8s", "use_authelia": True},
]
try:
    render_tag("authelia", stubs)
except RenderError as exc:
    print(f"refused: {exc}")
    sys.exit(0)
sys.exit(1)
"""
TIMEOUT = 300


def _python(code: str, *argv: str):
    return run(
        [sys.executable, "-c", BLOCK + code, *argv],
        cwd=REPO,
        env={"PYTHONPATH": str(SCRIPTS), "PATH": "/usr/bin:/bin"},
        check=False,
        timeout=TIMEOUT,
    )


def _stubs(tmp_path: Path) -> Path:
    stubs = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(stubs)]) == 0
    return stubs


def test_a_render_with_ansible_core_unimportable_is_clean(tmp_path):
    out = tmp_path / "out"
    done = _python(RENDER_ONE_TAG, str(out), str(_stubs(tmp_path)))
    assert done.returncode == 0, done.stderr
    assert (out / "cluster" / "littlelink").is_dir()


def test_a_render_that_imports_ansible_is_flagged(tmp_path):
    """The red half: the same render fails as soon as a module it imports reaches for ansible."""
    out = tmp_path / "out"
    done = _python(
        FILTERS_IMPORT_ANSIBLE + RENDER_ONE_TAG, str(out), str(_stubs(tmp_path))
    )
    assert done.returncode != 0
    assert "import of ansible halted" in done.stderr


def test_the_validator_with_ansible_core_unimportable_is_clean():
    done = _python(VALIDATE)
    assert done.returncode == 0, done.stderr


def test_a_host_package_render_with_ansible_core_unimportable_is_clean(tmp_path):
    out = tmp_path / "out"
    done = _python(RENDER_HOST, str(out))
    assert done.returncode == 0, done.stderr
    assert any(out.iterdir())


def test_a_filter_refusal_with_ansible_core_unimportable_is_a_render_error():
    stubs = shadow_diff.stub_vars()
    done = _python(REFUSE, json.dumps(stubs))
    assert done.returncode == 0, done.stderr
    assert "tier_priority_class: " in done.stdout
