"""Rule 5's walk over one fixture template, shared by the secret-check test modules."""

from pathlib import Path

from lib.repo_paths import SHARED_TPL
from renderer.renderer_lib.environment import renderer_env
from renderer.renderer_lib.jinja_aliases import scan_templates
from renderer.renderer_lib.jinja_uses import Policy, TemplateScan
from renderer.renderer_lib.secret_checks import PASSTHROUGH_FILTERS, SOPS_MANIFEST_KEYS


def scan_template(
    source: str,
    keys: frozenset[str],
    non_secret: frozenset[str] = frozenset(),
    passthrough: frozenset[str] = PASSTHROUGH_FILTERS,
    env=None,
) -> TemplateScan:
    """Rule 5's walk over `source`, with `passthrough` and `SOPS_MANIFEST_KEYS`."""
    env = env or renderer_env(SHARED_TPL)
    policy = Policy(passthrough, SOPS_MANIFEST_KEYS)
    path = Path("template.j2")
    return scan_templates({path: source}, keys, policy, env, {path: non_secret})[path]
