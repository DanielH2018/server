"""Ansible filter plugin holding the standalone-value guard's deploy-time check (target slice R2).

A standalone use is a placeholder that is the whole scalar of a field, such as
`email: ${email}`. Under Flux, a substituted standalone value loses the template's quotes and
is typed by its text, so a value such as `0123`, `yes` or `1e3` changes type. The shared
`k8s/manifests` role therefore refuses, before it renders, any value of a role's mapped key
that `scripts/lib/plain_scalar.py:refused` refuses. `scripts/renderer/standalone_map.py`
writes the map the role reads. `docs/homelab-render.md` is the operator's page.

The resolver lives in `scripts/lib/` because D3's `cluster_vars.py write` imports it too, so
the two producers cannot drift apart. Ansible imports a plugin by file path, so this module
loads the resolver by its path rather than putting `scripts/` on `sys.path`, where its `lib`
package would shadow any other top-level `lib` in the play.

`FilterModule.filters()` returns one literal dict, because
`scripts/deploy_tools/narrow_lib/filters.py:filter_names` reads the filter names statically.
"""

import functools
import importlib.util
from pathlib import Path

_RESOLVER = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "plain_scalar.py"


@functools.cache
def _resolver():
    # Loaded on the first call, not at import: Ansible imports every plugin in every play, so
    # a resolver that failed to load would otherwise stop plays that never run the guard.
    spec = importlib.util.spec_from_file_location("homelab_plain_scalar", _RESOLVER)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the standalone-value resolver from {_RESOLVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def homelab_standalone_refused(keys, values) -> list[str]:
    """The names in `keys` whose value Flux would not keep as the same string.

    `values` is a sequence parallel to `keys`, as `query('ansible.builtin.vars', *keys,
    default='')` returns it. A value that is not a string is refused, since its text is
    digits, a bool or a structure. An undefined key arrives as `''`, which the resolver
    refuses, so it is named too. The result holds names only, never a value, so an assert may
    print it whatever the run loaded.

    Raises:
        ValueError: `values` is of a different length from `keys`.
    """
    keys, values = [str(key) for key in keys], list(values)
    if len(values) != len(keys):
        raise ValueError(
            f"homelab_standalone_refused: {len(keys)} keys but {len(values)} values"
        )
    return [
        key
        for key, value in zip(keys, values, strict=True)
        if not isinstance(value, str) or _resolver().refused(value)
    ]


class FilterModule:
    # Unwrapped by `_as_filter`, as secret_stubs.py is: the one ValueError raised here already
    # names its filter.
    def filters(self):
        return {"homelab_standalone_refused": homelab_standalone_refused}
